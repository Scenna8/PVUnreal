"""
unreal_mesh_sender.py — ParaView plugin
========================================

Two filters under Filters → Unreal Engine:

  1. Bounding Box Finder
     Apply this to any dataset to send its spatial bounds to Unreal Engine
     immediately.  UE retains those bounds and uses them when building meshes
     so that every frame shares one consistent normalization transform.

     Place this filter directly after your data source (importer) so that the
     full computational-space bounds are known before any mesh data is sent.

  2. Mesh Sender
     Sends the active dataset (static or animated) to a running Unreal Engine
     instance over TCP.  Each mesh or frame is buffered by UE on receipt.
     After all meshes are sent, a final Update message tells UE to commit:
     it stamps the retained BB onto every buffered payload, computes the
     ParaView-BB → UE-display-box transform, and builds the mesh actors.

Protocol (all messages on port 9000)
-------------------------------------
  Every message is length-prefixed JSON:
    [4-byte big-endian length][UTF-8 JSON]
  Every message receives a 4-byte big-endian ACK from UE before the
  connection closes:
    bounds / mesh  → ACK on receipt
    update         → ACK after all meshes are built (deferred)

  Message types:
    {"type": "bounds", "min": {X,Y,Z}, "max": {X,Y,Z}}
    {"type": "mesh",   "id": ..., "verts": [...], "tris": [...], ...}
    {"type": "update"}

Install
-------
    Tools → Manage Plugins → Load New → select this file
    Tick "Auto Load" to load it automatically on startup.

Typical workflow
-----------------
    1. Load your data source in ParaView.
    2. Add Filters → Unreal Engine → Bounding Box Finder to it.  Apply.
       UE immediately stores the computational-space bounds.
    3. Select your original source again.
    4. Add Filters → Unreal Engine → Mesh Sender.  Set Scalar Field,
       Mesh ID, etc., then click Apply.
       Meshes are buffered by UE; when all are sent an Update is issued
       and UE builds the final mesh actors.

Requirements
------------
    pip install numpy matplotlib   (inside ParaView's Python environment)
    On macOS these are usually pre-installed.  If matplotlib is missing, the
    filter falls back to a greyscale colormap.
"""

from paraview.util.vtkAlgorithm import (
    VTKPythonAlgorithmBase, smproxy, smproperty, smdomain, smhint
)

try:
    import vtkmodules.all as vtk
    from vtkmodules.util.numpy_support import vtk_to_numpy
except ImportError:
    import vtk
    from vtk.util.numpy_support import vtk_to_numpy

import numpy as np
import socket
import struct
import json
import threading


# =============================================================================
# Geometry helpers
# =============================================================================

def _extract_surface_triangles(dataset):
    """
    Return a triangulated vtkPolyData.
    vtkPolyData is triangulated directly.
    Everything else goes through vtkDataSetSurfaceFilter first.
    """
    if dataset.IsA("vtkPolyData"):
        tri = vtk.vtkTriangleFilter()
        tri.SetInputData(dataset)
    else:
        surface = vtk.vtkDataSetSurfaceFilter()
        surface.SetInputData(dataset)
        surface.Update()
        tri = vtk.vtkTriangleFilter()
        tri.SetInputConnection(surface.GetOutputPort())
    tri.Update()
    return tri.GetOutput()


def _polydata_to_arrays(poly):
    """Return (points float (N,3), indices int (T,3))."""
    points     = vtk_to_numpy(poly.GetPoints().GetData()).astype(float)
    cells_flat = vtk_to_numpy(poly.GetPolys().GetData())
    indices    = cells_flat.reshape(-1, 4)[:, 1:]
    return points, indices


# =============================================================================
# Scalar field helpers
# =============================================================================

def _to_scalar(arr):
    """Convert a VTK array to a flat 1D numpy array (magnitude for vectors)."""
    data = vtk_to_numpy(arr).astype(float)
    return data if data.ndim == 1 else np.linalg.norm(data, axis=1)


def _get_scalar_field(dataset, poly, field_name=None):
    """
    Return (scalar_values, chosen_name).
    Searches point_data on the surface first, then cell_data on the original
    dataset.  If field_name is None or empty, picks the first available array.
    Raises ValueError if nothing is found.
    """
    def first_name(data_obj):
        return data_obj.GetArray(0).GetName() if data_obj.GetNumberOfArrays() > 0 else None

    pd   = poly.GetPointData()
    name = field_name or first_name(pd)
    if name:
        arr = pd.GetArray(name)
        if arr:
            return _to_scalar(arr), name

    cd   = dataset.GetCellData()
    name = field_name or first_name(cd)
    if name:
        c2p = vtk.vtkCellDataToPointData()
        c2p.SetInputData(dataset)
        c2p.Update()
        surf = vtk.vtkDataSetSurfaceFilter()
        surf.SetInputData(c2p.GetOutput())
        surf.Update()
        tri = vtk.vtkTriangleFilter()
        tri.SetInputConnection(surf.GetOutputPort())
        tri.Update()
        arr = tri.GetOutput().GetPointData().GetArray(name)
        if arr:
            return _to_scalar(arr), name

    raise ValueError(
        f"Scalar field {repr(field_name) if field_name else '(any)'} not found. "
        "Check available arrays in the Information panel."
    )


# =============================================================================
# Colormap helpers
# =============================================================================

_COLORMAP_RESOLUTION = 256


def _build_colormap_from_paraview(field_name, n=_COLORMAP_RESOLUTION):
    """
    Sample ParaView's active colour transfer function for field_name.
    Returns (colormap_dict, vmin, vmax), or (None, None, None) on failure.
    """
    try:
        from paraview.simple import GetColorTransferFunction
        proxy   = GetColorTransferFunction(field_name)
        vtk_ctf = proxy.GetClientSideObject()
        vmin, vmax = vtk_ctf.GetRange()

        flat = []
        for i in range(n):
            t = vmin + (vmax - vmin) * i / (n - 1) if vmax != vmin else vmin
            rgb = [0.0, 0.0, 0.0]
            vtk_ctf.GetColor(t, rgb)
            flat.extend([int(rgb[0] * 255), int(rgb[1] * 255), int(rgb[2] * 255), 255])

        return {"w": n, "h": 1, "data": flat}, vmin, vmax

    except Exception as e:
        print(f"[UnrealSender] Could not read ParaView CTF for '{field_name}': {e} "
              f"— falling back to matplotlib")
        return None, None, None


def _build_colormap_matplotlib(colormap_name="viridis", n=_COLORMAP_RESOLUTION):
    """Fallback: build a colormap from a matplotlib name."""
    try:
        import matplotlib.pyplot as plt
        cmap = plt.get_cmap(colormap_name)
        t    = np.linspace(0, 1, n)
        rgba = cmap(t)
        flat = []
        for r, g, b, _ in rgba:
            flat.extend([int(r * 255), int(g * 255), int(b * 255), 255])
    except ImportError:
        flat = []
        for i in range(n):
            v = int(i * 255 / (n - 1))
            flat.extend([v, v, v, 255])

    return {"w": n, "h": 1, "data": flat}


# =============================================================================
# TCP sender
# =============================================================================

def _recv_exact(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise EOFError(f"Connection closed after {len(buf)}/{n} bytes")
        buf += chunk
    return buf


def _query_volume_names(host, port=9001):
    """
    Fetch the alphabetically-sorted list of ADisplayVolumeActor names from
    Unreal's volume-query server (port 9001).
    Returns ['Volume 1'] on any failure so the dropdown is never empty.
    """
    try:
        with socket.create_connection((host, port), timeout=3) as s:
            n = struct.unpack('>I', _recv_exact(s, 4))[0]
            names = []
            for _ in range(n):
                name_len = struct.unpack('>I', _recv_exact(s, 4))[0]
                names.append(_recv_exact(s, name_len).decode('utf-8'))
            return names if names else ['Volume 1']
    except Exception:
        pass
    return ['Volume 1']


def _send_message(payload, host="127.0.0.1", port=9000, timeout=60):
    """
    Send a length-prefixed JSON message to Unreal and wait for the 4-byte ACK.

    Protocol:
      → [4-byte big-endian payload length][UTF-8 JSON]
      ← [4-byte big-endian status code]   (0 = OK)

    Raises OSError on connection failure and RuntimeError on a non-zero ACK.
    The Update message uses a longer timeout because UE defers the ACK until
    all mesh actors are built.
    """
    data   = json.dumps(payload).encode("utf-8")
    header = struct.pack(">I", len(data))
    with socket.create_connection((host, port), timeout=10) as sock:
        sock.sendall(header + data)
        # Wait up to `timeout` seconds for the ACK (Update can take a while).
        sock.settimeout(timeout)
        ack_bytes = b""
        while len(ack_bytes) < 4:
            chunk = sock.recv(4 - len(ack_bytes))
            if not chunk:
                raise OSError("Connection closed before ACK was received")
            ack_bytes += chunk
    code = struct.unpack(">I", ack_bytes)[0]
    if code != 0:
        raise RuntimeError(f"UE returned error ACK {code} for message type '{payload.get('type', '?')}'")


def _send_bounds(bounds_min, bounds_max, host="127.0.0.1", port=9000):
    """Send a bounds message and wait for the ACK."""
    _send_message({
        "type": "bounds",
        "min": {"X": float(bounds_min[0]), "Y": float(bounds_min[1]), "Z": float(bounds_min[2])},
        "max": {"X": float(bounds_max[0]), "Y": float(bounds_max[1]), "Z": float(bounds_max[2])},
    }, host=host, port=port)


def _send_mesh(vertices, triangles,
               scalars=None, scalar_min=None, scalar_max=None,
               color_map=None, mesh_id=None, volume_id=None,
               frame_index=None, total_frames=None, playback_fps=None,
               host="127.0.0.1", port=9000):
    """
    Send a mesh message and wait for the ACK (ACKed on receipt, not on build).
    Bounds are no longer included here — they are sent separately by
    BoundingBoxFinder via _send_bounds() before any mesh messages.
    """
    payload = {
        "type": "mesh",
        "verts": [{"X": v[0], "Y": v[1], "Z": v[2]} for v in vertices],
        "tris":  triangles,
    }
    if mesh_id:
        payload["id"] = mesh_id
    if volume_id is not None:
        payload["volume_id"] = volume_id
    if frame_index is not None:
        payload["frame"] = frame_index
    if total_frames is not None:
        payload["total_frames"] = total_frames
    if playback_fps is not None:
        payload["fps"] = playback_fps
    if scalars is not None:
        payload["scalars"]    = scalars
        payload["scalar_min"] = scalar_min if scalar_min is not None else float(min(scalars))
        payload["scalar_max"] = scalar_max if scalar_max is not None else float(max(scalars))
    if color_map:
        payload["colormap"] = color_map

    _send_message(payload, host=host, port=port)


def _send_update(host="127.0.0.1", port=9000):
    """
    Send the Update message and wait for the deferred ACK.
    UE sends the ACK only after all buffered meshes have been built, so this
    call may block for several seconds on large datasets — the timeout is 120 s.
    """
    _send_message({"type": "update"}, host=host, port=port, timeout=120)


# =============================================================================
# Shared state — written by BoundingBoxFinder, read by MeshSender
# =============================================================================

# Mutable container so BoundingBoxFinder can update the value without
# using the `global` keyword (which can clobber names in ParaView's shared
# Python namespace).  Keys: 'min' and 'max', each a (x,y,z) tuple or None.
_bounds_store = {'min': None, 'max': None}


# =============================================================================
# Filter 1 — Bounding Box Finder
# =============================================================================

@smproxy.filter(name="UnrealBoundingBoxFinder", label="Bounding Box Finder")
@smhint.xml('<ShowInMenu category="Unreal Engine"/>')
@smproperty.input(name="Input", port_index=0)
@smdomain.datatype(dataTypes=["vtkDataSet"])
class BoundingBoxFinderFilter(VTKPythonAlgorithmBase):
    """
    Computes the bounding box of the input dataset and outputs a wireframe
    box (vtkPolyData) you can see in the viewport.

    Connect this filter's output to the "Bounding Box" port of the Mesh Sender
    to give the animation a fixed, consistent normalization transform so the
    mesh doesn't jump between frames.

    Apply at whichever time step (or on whichever source) best represents the
    full spatial extent of your data — e.g. a pre-computed envelope mesh, a
    specific peak-size frame, or any dataset whose bounds you want to use.
    """

    def __init__(self):
        VTKPythonAlgorithmBase.__init__(
            self, nInputPorts=1, nOutputPorts=1, outputType="vtkDataSet"
        )
        self._host = "127.0.0.1"
        self._port = 9000

    @smproperty.stringvector(name="Host", label="Unreal Host",
                              default_values="127.0.0.1")
    def SetHost(self, v):
        self._host = v or "127.0.0.1"
        self.Modified()

    def GetHost(self):
        return self._host

    @smproperty.intvector(name="Port", label="Port", default_values=[9000])
    def SetPort(self, v):
        self._port = int(v)
        self.Modified()

    def GetPort(self):
        return self._port

    def FillOutputPortInformation(self, port, info):
        info.Set(vtk.vtkDataObject.DATA_TYPE_NAME(), "vtkDataObject")
        return 1

    def RequestDataObject(self, request, inInfo, outInfo):
        """Mirror the output type to match the input exactly."""
        inp = vtk.vtkDataObject.GetData(inInfo[0])
        if inp is None:
            return 0
        out = vtk.vtkDataObject.GetData(outInfo)
        if out is None or not out.IsA(inp.GetClassName()):
            out = inp.NewInstance()
            outInfo.GetInformationObject(0).Set(vtk.vtkDataObject.DATA_OBJECT(), out)
        return 1

    def RequestData(self, request, inInfo, outInfo):
        # Use vtkDataSet for GetBounds() — vtkDataObject doesn't have it.
        inp_ds  = vtk.vtkDataSet.GetData(inInfo[0])
        inp_obj = vtk.vtkDataObject.GetData(inInfo[0])
        out     = vtk.vtkDataObject.GetData(outInfo)

        if inp_obj is None:
            print("[BoundingBoxFinder] ERROR: No input.")
            return 0

        if inp_ds is not None:
            b = inp_ds.GetBounds()   # (xmin, xmax, ymin, ymax, zmin, zmax)
            bmin = (b[0], b[2], b[4])
            bmax = (b[1], b[3], b[5])
            # Keep the shared store for any legacy callers.
            _bounds_store['min'] = bmin
            _bounds_store['max'] = bmax
            print("[BoundingBoxFinder] Bounds computed:")
            print(f"  X: [{b[0]:.6g}, {b[1]:.6g}]")
            print(f"  Y: [{b[2]:.6g}, {b[3]:.6g}]")
            print(f"  Z: [{b[4]:.6g}, {b[5]:.6g}]")
            try:
                _send_bounds(bmin, bmax, host=self._host, port=self._port)
                print(f"[BoundingBoxFinder] Bounds sent to UE ({self._host}:{self._port}) — ACK received")
            except Exception as e:
                print(f"[BoundingBoxFinder] WARNING: Could not send bounds to UE: {e}")
        else:
            print("[BoundingBoxFinder] WARNING: Input is not a vtkDataSet — "
                  "cannot read bounds. Try applying to a vtu/vtp source directly.")

        # Pass the input through completely unchanged.
        out.ShallowCopy(inp_obj)
        return 1


# =============================================================================
# Filter 2 — Mesh Sender
# =============================================================================

@smproxy.filter(name="UnrealMeshSender", label="Mesh Sender")
@smhint.xml('<ShowInMenu category="Unreal Engine"/>')
@smproperty.input(name="Input", port_index=0)
@smdomain.datatype(dataTypes=["vtkDataSet"])
class UnrealMeshSenderFilter(VTKPythonAlgorithmBase):
    """
    Sends the input dataset to a running Unreal Engine instance over TCP.

    If a Bounding Box Finder filter has been applied anywhere in the session,
    its captured bounds are picked up automatically — no port connection needed.
    """

    def __init__(self):
        VTKPythonAlgorithmBase.__init__(
            self, nInputPorts=1, nOutputPorts=1, outputType="vtkPolyData"
        )
        self._field_name           = ""
        self._colormap             = "viridis"
        self._mesh_id              = ""
        self._volume_index         = 0
        self._volume_name          = ""
        self._last_volume_options  = []
        self._query_port           = 9001
        self._playback_fps         = 24.0
        self._host                 = "127.0.0.1"
        self._port                 = 9000
        self._animation_frames     = {}
        self._field_colormaps      = {}
        self._observed_ctf         = None
        self._ctf_observer_tag     = None
        self._updating_ctf         = False
        self._my_proxy             = None
        self._send_thread          = None
        self._cancel_flag          = threading.Event()

    # ------------------------------------------------------------------ helpers

    def _apply_colormap_to_paraview(self, field_name, colormap_name):
        try:
            import matplotlib.pyplot as plt
            from paraview.simple import GetColorTransferFunction

            cmap  = plt.get_cmap(colormap_name)
            proxy = GetColorTransferFunction(field_name)
            vmin, vmax = proxy.GetClientSideObject().GetRange()

            n = 64
            rgb_points = []
            for i in range(n):
                t   = i / (n - 1)
                val = vmin + (vmax - vmin) * t
                r, g, b, _ = cmap(t)
                rgb_points.extend([val, r, g, b])

            self._updating_ctf    = True
            proxy.RGBPoints       = rgb_points
            proxy.ColorSpace      = 'RGB'
            proxy.UpdateVTKObjects()
            self._updating_ctf    = False
            return True
        except Exception as e:
            self._updating_ctf = False
            print(f"[MeshSender] Could not apply colormap to ParaView CTF: {e}")
            return False

    def _find_my_proxy(self):
        try:
            from paraview.simple import GetSources
            for proxy in GetSources().values():
                try:
                    if proxy.GetClientSideObject() is self:
                        return proxy
                except Exception:
                    pass
        except Exception:
            pass
        return None

    def _on_ctf_modified(self, obj, event):
        if self._updating_ctf:
            return
        if self._my_proxy is None:
            self._my_proxy = self._find_my_proxy()
        if self._my_proxy is not None:
            try:
                self._my_proxy.UpdatePipeline()
                return
            except Exception:
                pass
        self.Modified()

    # ------------------------------------------------------------------ props

    @smproperty.xml("""
        <StringVectorProperty name="ScalarField" label="Scalar Field"
            command="SetScalarField"
            number_of_elements="1"
            default_values=""
            animateable="0">
            <ArrayListDomain name="array_list" none_string="(none)">
                <RequiredProperties>
                    <Property name="Input" function="Input"/>
                </RequiredProperties>
            </ArrayListDomain>
        </StringVectorProperty>
    """)
    def SetScalarField(self, v):
        if self._field_name:
            self._field_colormaps[self._field_name] = self._colormap
        self._field_name = v or ""
        if self._field_name in self._field_colormaps:
            self._colormap = self._field_colormaps[self._field_name]
        self._animation_frames.clear()
        self.Modified()

    def GetScalarField(self):
        return self._field_name

    @smproperty.xml("""
        <StringVectorProperty name="Colormap" label="Colormap"
            number_of_elements="1" default_values="viridis"
            command="SetColormap" animateable="0">
            <StringListDomain name="list">
                <String value="viridis"/>
                <String value="plasma"/>
                <String value="inferno"/>
                <String value="magma"/>
                <String value="cividis"/>
                <String value="turbo"/>
                <String value="jet"/>
                <String value="rainbow"/>
                <String value="coolwarm"/>
                <String value="RdBu"/>
                <String value="seismic"/>
                <String value="hot"/>
                <String value="cool"/>
                <String value="Reds"/>
                <String value="Blues"/>
                <String value="Greens"/>
                <String value="gray"/>
            </StringListDomain>
        </StringVectorProperty>
    """)
    def SetColormap(self, v):
        self._colormap = v or "viridis"
        if self._field_name:
            self._field_colormaps[self._field_name] = self._colormap
            self._apply_colormap_to_paraview(self._field_name, self._colormap)
        self._animation_frames.clear()
        self.Modified()

    def GetColormap(self):
        return self._colormap

    @smproperty.stringvector(name="MeshId", label="Mesh ID (blank = filename)",
                              default_values="")
    def SetMeshId(self, v):
        self._mesh_id = v or ""
        self._animation_frames.clear()
        self.Modified()

    def GetMeshId(self):
        return self._mesh_id

    @smproperty.xml("""
        <StringVectorProperty name="VolumeIdOptions"
                              command="GetVolumeIdOptions"
                              information_only="1"
                              number_of_elements_per_command="1"
                              repeatable_command="1">
            <StringListDomain name="list"/>
        </StringVectorProperty>
    """)
    def GetVolumeIdOptions(self):
        self._last_volume_options = _query_volume_names(self._host, self._query_port)
        return self._last_volume_options

    @smproperty.xml("""
        <StringVectorProperty name="VolumeId"
                              label="Display Volume"
                              command="SetVolumeId"
                              number_of_elements="1"
                              default_values="Volume 1">
            <StringListDomain name="list">
                <RequiredProperties>
                    <Property function="ArrayList" name="VolumeIdOptions"/>
                </RequiredProperties>
            </StringListDomain>
        </StringVectorProperty>
    """)
    def SetVolumeId(self, v):
        self._volume_name = v
        try:
            self._volume_index = self._last_volume_options.index(v)
        except ValueError:
            self._volume_index = 0
        self.Modified()

    def GetVolumeId(self):
        return self._volume_name

    @smproperty.doublevector(name="PlaybackFPS", label="Playback FPS",
                              default_values=[24.0])
    def SetPlaybackFPS(self, v):
        self._playback_fps = float(v) if v > 0 else 24.0
        self.Modified()

    def GetPlaybackFPS(self):
        return self._playback_fps

    @smproperty.xml("""
        <IntVectorProperty name="CancelSend" label="Cancel Send"
            command="SetCancelSend"
            number_of_elements="1"
            default_values="0"
            animateable="0">
            <BooleanDomain name="bool"/>
            <Documentation>
                Set to cancel an in-progress animation send.
                The current frame finishes (waiting for its ACK), then the
                send loop exits cleanly.
            </Documentation>
        </IntVectorProperty>
    """)
    def SetCancelSend(self, v):
        if v:
            self._cancel_flag.set()
            print("[MeshSender] Cancel requested — will stop after current frame")

    def GetCancelSend(self):
        return 0   # always reads back as unchecked

    @smproperty.stringvector(name="Host", label="Unreal Host",
                              default_values="127.0.0.1")
    def SetHost(self, v):
        self._host = v or "127.0.0.1"
        self._animation_frames.clear()
        self.Modified()

    def GetHost(self):
        return self._host

    @smproperty.intvector(name="Port", label="Port", default_values=[9000])
    def SetPort(self, v):
        self._port = int(v)
        self._animation_frames.clear()
        self.Modified()

    def GetPort(self):
        return self._port

    # ----------------------------------------------------------- pipeline

    @staticmethod
    def _get_time_steps(info_obj):
        key = vtk.vtkStreamingDemandDrivenPipeline.TIME_STEPS()
        if info_obj.Has(key):
            return [info_obj.Get(key, i) for i in range(info_obj.Length(key))]
        return []

    @staticmethod
    def _get_current_time(info_obj):
        key = vtk.vtkStreamingDemandDrivenPipeline.UPDATE_TIME_STEP()
        if info_obj.Has(key):
            return info_obj.Get(key)
        return None

    def RequestData(self, request, inInfo, outInfo):
        print("[MeshSender] ---- RequestData called ----")

        inp = vtk.vtkDataSet.GetData(inInfo[0])
        out = vtk.vtkPolyData.GetData(outInfo)

        if inp is None:
            print("[MeshSender] ERROR: No input dataset.")
            return 0

        print(f"[MeshSender] Input: {inp.GetClassName()}, "
              f"{inp.GetNumberOfPoints()} pts, {inp.GetNumberOfCells()} cells")

        # Bounds are now sent directly by BoundingBoxFinder to UE.
        # Log a reminder if the shared store is empty (BB filter not applied).
        if _bounds_store['min'] is None:
            print("[MeshSender] NOTE: No bounds in shared store — "
                  "ensure Bounding Box Finder has been applied and sent bounds to UE")

        # --- Detect animation ---
        info_obj   = inInfo[0].GetInformationObject(0)
        time_steps = self._get_time_steps(info_obj)
        cur_time   = self._get_current_time(info_obj)
        n_steps    = len(time_steps)
        is_anim    = n_steps > 1

        if is_anim:
            frame_idx = min(range(n_steps),
                            key=lambda i: abs(time_steps[i] - (cur_time or 0.0)))
            print(f"[MeshSender] Animation — frame {frame_idx + 1}/{n_steps} "
                  f"(t={cur_time:.4g})")
            # Disconnect the CTF observer — it calls UpdatePipeline() on every
            # colormap change, which keeps driving the pipeline even after the
            # ParaView Stop button is clicked.  It's only needed for static meshes.
            if self._observed_ctf is not None:
                self._observed_ctf.RemoveObserver(self._ctf_observer_tag)
                self._observed_ctf    = None
                self._ctf_observer_tag = None
        else:
            frame_idx = -1
            print("[MeshSender] Static")

        # --- Surface ---
        try:
            poly = _extract_surface_triangles(inp)
        except Exception as e:
            print(f"[MeshSender] ERROR during surface extraction: {e}")
            return 0

        n_verts = poly.GetNumberOfPoints()
        n_tris  = poly.GetNumberOfCells()
        print(f"[MeshSender] Surface: {n_verts} verts, {n_tris} triangles")

        if n_verts == 0 or n_tris == 0:
            print("[MeshSender] WARNING: No geometry — skipping")
            if is_anim:
                self._animation_frames[frame_idx] = None
                if len(self._animation_frames) == n_steps:
                    self._launch_send_thread(n_steps)
            return 1

        out.ShallowCopy(poly)

        points, indices = _polydata_to_arrays(poly)
        verts = [(float(p[0]), float(p[1]), float(p[2])) for p in points]
        tris  = indices.flatten().tolist()

        # --- Scalar field ---
        field_name = self._field_name.strip() or None
        try:
            scalar_values, chosen = _get_scalar_field(inp, poly, field_name)
            print(f"[MeshSender] Scalar '{chosen}': "
                  f"min={scalar_values.min():.4g}, max={scalar_values.max():.4g}")
        except ValueError as e:
            print(f"[MeshSender] Warning: {e} — no color")
            scalar_values = None
            chosen = None

        # --- Animation buffering vs. static send ---
        if is_anim:
            self._animation_frames[frame_idx] = {
                'verts':   verts,
                'tris':    tris,
                'scalars': scalar_values,
                'chosen':  chosen,
            }
            print(f"[MeshSender] Buffered frame {frame_idx} "
                  f"({len(self._animation_frames)}/{n_steps})")

            if len(self._animation_frames) == n_steps:
                self._launch_send_thread(n_steps)
        else:
            if scalar_values is not None:
                cmap, ctf_vmin, ctf_vmax = _build_colormap_from_paraview(chosen)
                if cmap is not None:
                    vmin, vmax = ctf_vmin, ctf_vmax
                else:
                    cmap = _build_colormap_matplotlib(self._colormap)
                    vmin = float(scalar_values.min())
                    vmax = float(scalar_values.max())
                scalars_list = scalar_values.tolist()
                try:
                    from paraview.simple import GetColorTransferFunction
                    vtk_ctf = GetColorTransferFunction(chosen).GetClientSideObject()
                    if self._observed_ctf is not vtk_ctf:
                        if self._observed_ctf is not None:
                            self._observed_ctf.RemoveObserver(self._ctf_observer_tag)
                        self._ctf_observer_tag = vtk_ctf.AddObserver(
                            'ModifiedEvent', self._on_ctf_modified)
                        self._observed_ctf = vtk_ctf
                except Exception:
                    pass
            else:
                scalars_list = None
                vmin = vmax = None
                cmap = None

            mesh_id   = self._mesh_id.strip() or None
            volume_id = self._volume_index
            print(f"[MeshSender] Sending static mesh '{mesh_id or 'default'}' "
                  f"to {self._host}:{self._port} ...")
            try:
                _send_mesh(verts, tris,
                           scalars=scalars_list, scalar_min=vmin, scalar_max=vmax,
                           color_map=cmap, mesh_id=mesh_id, volume_id=volume_id,
                           host=self._host, port=self._port)
                print(f"[MeshSender] Mesh buffered by UE — sending Update ...")
                _send_update(host=self._host, port=self._port)
                print(f"[MeshSender] SUCCESS — Update ACK received "
                      f"({len(verts)} verts, {len(tris)//3} tris)")
            except OSError as e:
                print(f"[MeshSender] ERROR: Connection failed — {e}")
            except Exception as e:
                print(f"[MeshSender] ERROR: {type(e).__name__}: {e}")

        return 1

    def _launch_send_thread(self, total_frames):
        """
        Cancel any in-progress send, then start a new daemon thread to send
        all buffered frames.  Returns immediately so ParaView's pipeline thread
        (and the GUI) stays responsive.

        Also stops ParaView's animation scene — all frames are collected and
        the UE side plays independently, so there is no reason to keep looping.
        Without this, loop mode would restart the animation immediately and the
        Stop button would have no effect.
        """
        if self._send_thread and self._send_thread.is_alive():
            print("[MeshSender] Previous send still running — cancelling it first")
            self._cancel_flag.set()
            self._send_thread.join(timeout=5)

        frames_snapshot = dict(self._animation_frames)
        self._animation_frames = {}

        # Stop the ParaView animation before launching the send thread so the
        # loop doesn't restart and begin collecting frames all over again.
        try:
            from paraview.simple import GetAnimationScene
            GetAnimationScene().Stop()
            print("[MeshSender] ParaView animation stopped — all frames collected")
        except Exception as e:
            print(f"[MeshSender] Could not stop animation scene: {e}")

        self._send_thread = threading.Thread(
            target=self._send_buffered_animation,
            args=(total_frames, frames_snapshot),
            daemon=True,
        )
        self._send_thread.start()
        print(f"[MeshSender] Send thread started for {total_frames} frames")

    def _send_buffered_animation(self, total_frames, frames_snapshot):
        """
        Runs on a daemon thread.  Sends each frame as a mesh message (ACKed on
        receipt), checks the cancel flag between frames, then sends a single
        Update message and waits for the deferred ACK from UE.
        """
        self._cancel_flag.clear()

        mesh_id   = self._mesh_id.strip() or None
        volume_id = self._volume_index
        fps       = self._playback_fps

        valid_frames = [(i, frames_snapshot[i])
                        for i in range(total_frames)
                        if frames_snapshot.get(i) is not None]
        all_scalars = [f['scalars'] for _, f in valid_frames
                       if f.get('scalars') is not None]

        if all_scalars:
            g_min = float(min(s.min() for s in all_scalars))
            g_max = float(max(s.max() for s in all_scalars))
            cmap  = _build_colormap_matplotlib(self._colormap)
            print(f"[MeshSender] Global scalar range: {g_min:.4g} – {g_max:.4g}")
        else:
            g_min = g_max = 0.0
            cmap = None

        n_valid = len(valid_frames)
        print(f"[MeshSender] Sending {n_valid} frames  "
              f"mesh='{mesh_id or 'default'}'  fps={fps:.1f}")

        try:
            for seq_idx, (_, frame) in enumerate(valid_frames):
                if self._cancel_flag.is_set():
                    print(f"[MeshSender] Cancelled after {seq_idx}/{n_valid} frames")
                    return

                scalars = frame.get('scalars')
                _send_mesh(
                    frame['verts'], frame['tris'],
                    scalars      = scalars.tolist() if scalars is not None else None,
                    scalar_min   = g_min,
                    scalar_max   = g_max,
                    color_map    = (cmap if seq_idx == 0 else None),
                    mesh_id      = mesh_id,
                    volume_id    = volume_id,
                    frame_index  = seq_idx,
                    total_frames = n_valid,
                    playback_fps = fps,
                    host         = self._host,
                    port         = self._port,
                )
                print(f"[MeshSender]   frame {seq_idx + 1}/{n_valid} buffered by UE")

            print(f"[MeshSender] All frames buffered — sending Update ...")
            _send_update(host=self._host, port=self._port)
            print(f"[MeshSender] Animation complete — Update ACK received "
                  f"→ {self._host}:{self._port}")
        except OSError as e:
            print(f"[MeshSender] ERROR: Connection failed — {e}")
        except Exception as e:
            print(f"[MeshSender] ERROR: {type(e).__name__}: {e}")
