"""
unreal_mesh_sender.py — ParaView plugin
========================================

Sends the active dataset to a running Unreal Engine instance over TCP.
The Unreal project must have MeshBuilderSubsystem active and listening on
the configured port (default 9000).

Install
-------
    Tools → Manage Plugins → Load New → select this file
    Tick "Auto Load" to load it automatically on startup.

Usage
-----
    1. Load a .vtu / .vtp file in ParaView.
    2. Select it in the Pipeline Browser.
    3. Filters → Unreal Engine → Send to Unreal Engine
    4. Set properties (Scalar Field, Mesh ID, etc.) and click Apply.
    5. Press Tab in Unreal to cycle between meshes if you have sent several.

Output
------
    The filter outputs the triangulated surface that was sent (vtkPolyData),
    so you can inspect it in ParaView as a confirmation of what Unreal received.

Requirements
------------
    pip install numpy vtk matplotlib   (inside ParaView's Python environment)
    On macOS these are usually pre-installed. If matplotlib is missing, the
    filter still works but uses a greyscale fallback colormap.
"""

from paraview.util.vtkAlgorithm import (
    VTKPythonAlgorithmBase, smproxy, smproperty, smdomain, smhint
)

# ParaView 5.10+ uses vtkmodules; older builds still ship the monolithic vtk.
# Try vtkmodules first and fall back to the legacy package.
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


# =============================================================================
# Geometry helpers
# =============================================================================

def _extract_surface_triangles(dataset):
    """
    Return a triangulated vtkPolyData.
    vtkPolyData is triangulated directly.
    Everything else (vtkUnstructuredGrid, vtkStructuredGrid, etc.) goes
    through vtkDataSetSurfaceFilter first to extract the outer surface.
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
    indices    = cells_flat.reshape(-1, 4)[:, 1:]   # drop leading '3'
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
    Searches point_data on the surface first, then cell_data on the original dataset.
    If field_name is None or empty, picks the first available array.
    Raises ValueError if nothing is found.
    """
    def first_name(data_obj):
        return data_obj.GetArray(0).GetName() if data_obj.GetNumberOfArrays() > 0 else None

    # Point data already on the surface
    pd   = poly.GetPointData()
    name = field_name or first_name(pd)
    if name:
        arr = pd.GetArray(name)
        if arr:
            return _to_scalar(arr), name

    # Cell data: propagate to points then re-extract surface
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

    Returns (colormap_dict, vmin, vmax) so the caller can normalise scalar
    values against the same range — ensuring the colour mapping in Unreal
    matches exactly what ParaView displays.

    Returns (None, None, None) if the CTF is unavailable.
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
    """
    Fallback: build a colormap from a matplotlib name.
    Used when ParaView has no CTF for the chosen field.
    Falls back to greyscale if matplotlib is not installed.
    """
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
    """Read exactly n bytes from sock, raising EOFError if the connection closes early."""
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise EOFError(f"Connection closed after {len(buf)}/{n} bytes")
        buf += chunk
    return buf


def _query_volume_names(host, port=9001):
    """
    Connect to Unreal's volume-query server and return the alphabetically-sorted
    list of ADisplayVolumeActor VolumeName strings.

    Wire format (big-endian):
        [4-byte count N]
        for each name:
            [4-byte UTF-8 byte length]
            [UTF-8 bytes]

    Returns a list of strings, or ['Volume 1'] on any failure so the dropdown
    is never empty.
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


def _send_mesh(vertices, triangles,
               scalars=None, scalar_min=None, scalar_max=None,
               color_map=None, mesh_id=None, volume_id=None,
               frame_index=None, total_frames=None, playback_fps=None,
               host="127.0.0.1", port=9000):
    """
    Send a length-prefixed JSON payload to Unreal.
    Protocol: 4-byte big-endian length header followed by UTF-8 JSON.

    Raw vertex positions are sent without normalization — Unreal centres and
    scales them to ±100 cm.  Normals and UVs are also computed Unreal-side from
    the scalar values and their range.
    """
    payload_dict = {
        "verts": [{"X": v[0], "Y": v[1], "Z": v[2]} for v in vertices],
        "tris":  triangles,
    }
    if mesh_id:
        payload_dict["id"] = mesh_id
    if volume_id is not None:
        payload_dict["volume_id"] = volume_id
    if frame_index is not None:
        payload_dict["frame"] = frame_index
    if total_frames is not None:
        payload_dict["total_frames"] = total_frames
    if playback_fps is not None:
        payload_dict["fps"] = playback_fps
    if scalars is not None:
        payload_dict["scalars"]    = scalars
        payload_dict["scalar_min"] = scalar_min if scalar_min is not None else float(min(scalars))
        payload_dict["scalar_max"] = scalar_max if scalar_max is not None else float(max(scalars))
    if color_map:
        payload_dict["colormap"] = color_map

    payload = json.dumps(payload_dict).encode("utf-8")
    header  = struct.pack(">I", len(payload))
    with socket.create_connection((host, port), timeout=10) as sock:
        sock.sendall(header + payload)


# =============================================================================
# ParaView filter
# =============================================================================

@smproxy.filter(name="UnrealMeshSender", label="Send to Unreal Engine")
@smhint.xml('<AutoApply frequency="1"/>')
@smproperty.input(name="Input", port_index=0)
@smdomain.datatype(dataTypes=["vtkDataSet"])
class UnrealMeshSenderFilter(VTKPythonAlgorithmBase):
    """
    ParaView filter that sends the active dataset to Unreal Engine over TCP.
    The filter passes through a triangulated surface as its output so you can
    inspect what was transmitted.
    """

    def __init__(self):
        VTKPythonAlgorithmBase.__init__(
            self, nInputPorts=1, nOutputPorts=1, outputType="vtkPolyData"
        )
        self._field_name  = ""
        self._colormap    = "viridis"
        self._mesh_id     = ""
        self._volume_index = 0          # 0-based index into the sorted name list
        self._volume_name  = ""         # display string; kept for GetVolumeId
        self._last_volume_options = []  # cached from GetVolumeIdOptions
        self._query_port  = 9001
        self._playback_fps = 24.0
        # Animation buffering: keyed by frame index (int → dict of arrays).
        # Cleared whenever a property changes (norm_dirty) or a new sequence starts.
        self._animation_frames = {}
        self._host        = "127.0.0.1"
        self._port        = 9000
        # Per-field colormap memory: {field_name: colormap_name}
        self._field_colormaps  = {}
        # CTF observer state.
        self._observed_ctf     = None
        self._ctf_observer_tag = None
        self._updating_ctf     = False   # True while we are modifying the CTF ourselves
        # Cached proxy — looked up once so the observer can call UpdatePipeline().
        self._my_proxy         = None

    def _apply_colormap_to_paraview(self, field_name, colormap_name):
        """
        Write a matplotlib colormap into ParaView's CTF for field_name.
        Sets _updating_ctf while doing so to suppress the observer and prevent
        a double send (the normal Apply pipeline execution handles the send).
        Returns True on success.
        """
        try:
            import matplotlib.pyplot as plt
            import numpy as np
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
            print(f"[UnrealSender] Could not apply colormap to ParaView CTF: {e}")
            return False

    def _find_my_proxy(self):
        """Locate this algorithm's ParaView proxy by scanning GetSources()."""
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
        """Fired whenever the active colour transfer function changes.
        Calls UpdatePipeline() directly so the filter re-executes — and
        resends to Unreal — without the user having to click Apply.
        Ignored when we are the ones modifying the CTF (avoids double send)."""
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
        self.Modified()  # fallback if proxy lookup failed

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
        # Save the colormap currently shown for the outgoing field.
        if self._field_name:
            self._field_colormaps[self._field_name] = self._colormap

        self._field_name = v or ""

        # Restore the saved colormap for the incoming field (if any).
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
        # Remember this choice for the current field.
        if self._field_name:
            self._field_colormaps[self._field_name] = self._colormap
        # Write directly into ParaView's CTF for this field so the viewport
        # updates to match. The CTF observer is suppressed via _updating_ctf,
        # so self.Modified() is the sole trigger for pipeline re-execution.
        if self._field_name:
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

    # ---- Display Volume: dynamic dropdown populated by querying Unreal port 9001 ----
    #
    # ParaView calls GetVolumeIdOptions() to populate the StringListDomain, then
    # shows the result as a dropdown of human-readable VolumeName strings.
    # The list is re-fetched each time the Properties panel refreshes.
    # Internally the selected name's 0-based position in the list is sent as the
    # integer volume_id — the user never sees a number.

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
        """Called by ParaView to populate the Display Volume dropdown."""
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
        """Return the full list of available time step values, or []."""
        key = vtk.vtkStreamingDemandDrivenPipeline.TIME_STEPS()
        if info_obj.Has(key):
            return [info_obj.Get(key, i) for i in range(info_obj.Length(key))]
        return []

    @staticmethod
    def _get_current_time(info_obj):
        """Return the currently requested time value, or None."""
        key = vtk.vtkStreamingDemandDrivenPipeline.UPDATE_TIME_STEP()
        if info_obj.Has(key):
            return info_obj.Get(key)
        return None

    def RequestData(self, request, inInfo, outInfo):
        print("[UnrealSender] ---- RequestData called ----")

        # --- Get input ---
        inp = vtk.vtkDataSet.GetData(inInfo[0])
        out = vtk.vtkPolyData.GetData(outInfo)

        if inp is None:
            print("[UnrealSender] ERROR: Input dataset is None — is a source selected?")
            return 0

        print(f"[UnrealSender] Input: {inp.GetClassName()}, "
              f"{inp.GetNumberOfPoints()} pts, {inp.GetNumberOfCells()} cells")

        # --- Detect animation time steps ---
        info_obj   = inInfo[0].GetInformationObject(0)
        time_steps = self._get_time_steps(info_obj)
        cur_time   = self._get_current_time(info_obj)
        n_steps    = len(time_steps)
        is_anim    = n_steps > 1

        if is_anim:
            # Map current time to a frame index (nearest match).
            frame_idx = min(range(n_steps),
                            key=lambda i: abs(time_steps[i] - (cur_time or 0.0)))
            print(f"[UnrealSender] Animation mode — frame {frame_idx + 1} / {n_steps} "
                  f"(t={cur_time:.4g})")
        else:
            frame_idx = -1
            print("[UnrealSender] Static mode")

        # --- Surface extraction + triangulation ---
        try:
            poly = _extract_surface_triangles(inp)
        except Exception as e:
            print(f"[UnrealSender] ERROR during surface extraction: {e}")
            return 0

        n_verts = poly.GetNumberOfPoints()
        n_tris  = poly.GetNumberOfCells()
        print(f"[UnrealSender] Surface: {n_verts} verts, {n_tris} triangles")

        if n_verts == 0 or n_tris == 0:
            print("[UnrealSender] WARNING: No geometry at this time step — skipping")
            if is_anim:
                # Still store a sentinel so the frame count tracks correctly.
                self._animation_frames[frame_idx] = None
                if len(self._animation_frames) == n_steps:
                    self._send_buffered_animation(n_steps)
            return 1

        out.ShallowCopy(poly)

        # --- Geometry arrays ---
        # Send raw coordinates — Unreal normalises to ±100 cm.
        points, indices = _polydata_to_arrays(poly)
        verts = [(float(p[0]), float(p[1]), float(p[2])) for p in points]
        tris  = indices.flatten().tolist()

        # --- Scalar field ---
        pd = poly.GetPointData()
        available = [pd.GetArray(i).GetName() for i in range(pd.GetNumberOfArrays())]
        print(f"[UnrealSender] Point arrays on surface: {available}")

        field_name = self._field_name.strip() or None
        try:
            scalar_values, chosen = _get_scalar_field(inp, poly, field_name)
            print(f"[UnrealSender] Scalar '{chosen}': "
                  f"n={len(scalar_values)}, "
                  f"min={scalar_values.min():.4g}, max={scalar_values.max():.4g}")
        except ValueError as e:
            print(f"[UnrealSender] Warning: {e} — no color")
            scalar_values = None
            chosen = None

        # --- Branch: animation buffering vs. immediate static send ---
        if is_anim:
            # Buffer raw scalars; global range is resolved once all frames arrive.
            self._animation_frames[frame_idx] = {
                'verts':   verts,
                'tris':    tris,
                'scalars': scalar_values,
                'chosen':  chosen,
            }
            print(f"[UnrealSender] Buffered frame {frame_idx} "
                  f"({len(self._animation_frames)} / {n_steps} collected)")

            if len(self._animation_frames) == n_steps:
                self._send_buffered_animation(n_steps)
        else:
            # Static send — resolve colormap and scalar range.
            if scalar_values is not None:
                # Try to get colormap + range from ParaView's active CTF so the
                # Unreal colour mapping matches the ParaView viewport exactly.
                cmap, ctf_vmin, ctf_vmax = _build_colormap_from_paraview(chosen)
                if cmap is not None:
                    vmin, vmax = ctf_vmin, ctf_vmax
                else:
                    cmap = _build_colormap_matplotlib(self._colormap)
                    vmin = float(scalar_values.min())
                    vmax = float(scalar_values.max())
                scalars_list = scalar_values.tolist()
                # Attach CTF observer so external CTF edits trigger a re-send.
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
            print(f"[UnrealSender] Mesh ID: '{mesh_id or 'default'}'  "
                  f"Volume: '{self._volume_name}' (index {volume_id})")
            print(f"[UnrealSender] Connecting to {self._host}:{self._port} ...")
            try:
                _send_mesh(verts, tris,
                           scalars=scalars_list, scalar_min=vmin, scalar_max=vmax,
                           color_map=cmap, mesh_id=mesh_id,
                           volume_id=volume_id,
                           host=self._host, port=self._port)
                print(f"[UnrealSender] SUCCESS — {len(verts)} verts, "
                      f"{len(tris) // 3} tris sent")
            except OSError as e:
                print(f"[UnrealSender] ERROR: Connection failed — {e}")
            except Exception as e:
                print(f"[UnrealSender] ERROR: {type(e).__name__}: {e}")

        return 1

    def _send_buffered_animation(self, total_frames):
        """
        Called once all animation frames have been buffered.

        Computes a global scalar range across every frame so colours are
        consistent throughout the animation, then sends each frame to Unreal
        as an indexed animation packet.  Unreal starts looped playback
        automatically once all packets arrive.
        """
        print(f"[UnrealSender] All {total_frames} frames collected — sending animation ...")

        mesh_id   = self._mesh_id.strip()   or None
        volume_id = self._volume_index
        fps       = self._playback_fps

        # Collect scalar arrays across all frames (skip sentinels / empty frames).
        valid_frames = [(i, self._animation_frames[i])
                        for i in range(total_frames)
                        if self._animation_frames.get(i) is not None]
        all_scalars = [f['scalars'] for _, f in valid_frames
                       if f.get('scalars') is not None]

        if all_scalars:
            g_min = float(min(s.min() for s in all_scalars))
            g_max = float(max(s.max() for s in all_scalars))
            cmap  = _build_colormap_matplotlib(self._colormap)
            print(f"[UnrealSender] Global scalar range: {g_min:.4g} – {g_max:.4g}")
        else:
            g_min = g_max = 0.0
            cmap = None

        n_valid = len(valid_frames)
        print(f"[UnrealSender] Sending {n_valid} non-empty frames  "
              f"mesh='{mesh_id or 'default'}'  "
              f"volume='{self._volume_name}' (index {volume_id})  "
              f"fps={fps:.1f}")

        try:
            sent = 0
            for seq_idx, (_, frame) in enumerate(valid_frames):
                scalars = frame.get('scalars')
                _send_mesh(
                    frame['verts'], frame['tris'],
                    scalars    = scalars.tolist() if scalars is not None else None,
                    scalar_min = g_min,
                    scalar_max = g_max,
                    color_map  = (cmap if seq_idx == 0 else None),
                    mesh_id    = mesh_id,
                    volume_id  = volume_id,
                    frame_index   = seq_idx,
                    total_frames  = n_valid,
                    playback_fps  = fps,
                    host = self._host,
                    port = self._port,
                )
                sent += 1
                print(f"[UnrealSender]   frame {sent} / {n_valid} sent")

            print(f"[UnrealSender] Animation complete — "
                  f"{sent} frames → {self._host}:{self._port}")
        except OSError as e:
            print(f"[UnrealSender] ERROR: Connection failed — {e}")
        except Exception as e:
            print(f"[UnrealSender] ERROR: {type(e).__name__}: {e}")

        # Clear the buffer so a re-play of the ParaView animation re-sends cleanly.
        self._animation_frames.clear()
