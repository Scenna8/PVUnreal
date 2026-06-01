"""
vtu_sender.py

Loads a VTU (VTK Unstructured Grid) or VTP (VTK PolyData) file, extracts
mesh geometry and scalar field data, maps the scalar field through a colormap,
and sends the result to a running Unreal Engine instance via sender.py.

Usage:
    python vtu_sender.py mesh.vtu
    python vtu_sender.py mesh.vtp
    python vtu_sender.py mesh.vtu --field pressure --colormap viridis
    python vtu_sender.py mesh.vtu --list-fields

Requires:
    pip install vtk numpy matplotlib
"""

import sys
import argparse
import time
import numpy as np
import vtk
from vtk.util.numpy_support import vtk_to_numpy
import matplotlib.pyplot as plt

from sender import send_mesh, make_color_map


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------

def load_file(path):
    """
    Read a .vtu or .vtp file and return the VTK dataset.
    Returns (dataset, file_type) where file_type is 'vtu' or 'vtp'.
    """
    ext = path.rsplit(".", 1)[-1].lower()

    if ext == "vtu":
        reader = vtk.vtkXMLUnstructuredGridReader()
        reader.SetFileName(path)
        reader.Update()
        return reader.GetOutput(), "vtu"

    elif ext == "vtp":
        reader = vtk.vtkXMLPolyDataReader()
        reader.SetFileName(path)
        reader.Update()
        return reader.GetOutput(), "vtp"

    else:
        raise ValueError(f"Unsupported file type '.{ext}'. Expected .vtu or .vtp.")


def extract_surface_triangles(dataset, file_type):
    """
    Return a triangulated vtkPolyData from either a vtkUnstructuredGrid (vtu)
    or a vtkPolyData (vtp).

    VTU: run vtkDataSetSurfaceFilter first to extract the outer surface, then
         triangulate. Handles volumetric cells (tetra, hex, etc.) automatically.

    VTP: already PolyData — just triangulate quads/polygons and return.
    """
    if file_type == "vtu":
        surface = vtk.vtkDataSetSurfaceFilter()
        surface.SetInputData(dataset)
        surface.Update()
        tri_input = surface.GetOutputPort()
    else:
        # VTP is already PolyData — feed directly to the triangle filter
        tri_input = dataset

    triangulate = vtk.vtkTriangleFilter()
    if file_type == "vtu":
        triangulate.SetInputConnection(tri_input)
    else:
        triangulate.SetInputData(tri_input)
    triangulate.Update()

    return triangulate.GetOutput()


def polydata_to_arrays(poly):
    """
    Convert a triangulated vtkPolyData into plain numpy arrays.

    Returns:
        points  — float array (N, 3)
        indices — int array   (T, 3)  — shape (0, 3) when there are no triangles
    """
    # Points
    points = vtk_to_numpy(poly.GetPoints().GetData()).astype(float)

    # Cells — the cell array is stored as [3, i0, i1, i2,  3, i0, i1, i2, ...]
    cells_flat = vtk_to_numpy(poly.GetPolys().GetData())

    if cells_flat.size == 0:
        return points, np.empty((0, 3), dtype=np.int64)

    indices    = cells_flat.reshape(-1, 4)[:, 1:]   # drop the leading '3'
    return points, indices


def compute_normals(poly):
    """
    Compute smooth per-vertex surface normals on a triangulated vtkPolyData,
    guaranteed to point outward. Always returns a list — never None.

    AutoOrientNormalsOn() is unreliable for open/non-closed surfaces (common
    in CFD — inlets, outlets, cuts leave holes). Instead we compute normals
    with ConsistencyOn() to make them all agree with each other, then do our
    own outward orientation check: sample the dot product of each normal
    against the vector from the mesh centroid to that vertex. If the average
    is negative the whole batch is facing inward and we flip them.

    SplittingOff() preserves the vertex count so indices from
    polydata_to_arrays() stay valid.

    If vtkPolyDataNormals returns nothing, falls back to averaging triangle
    cross-products per vertex in numpy.
    """
    pts_np = vtk_to_numpy(poly.GetPoints().GetData()).astype(float)

    nf = vtk.vtkPolyDataNormals()
    nf.SetInputData(poly)
    nf.ComputePointNormalsOn()
    nf.ComputeCellNormalsOff()
    nf.SplittingOff()    # preserve vertex count — no edge splitting
    nf.ConsistencyOn()   # make all normals agree in orientation
    # AutoOrientNormalsOn() deliberately omitted — unreliable for open meshes
    nf.Update()

    normals_vtk = nf.GetOutput().GetPointData().GetNormals()

    if normals_vtk is not None:
        normals_np = vtk_to_numpy(normals_vtk).astype(float)
    else:
        # Fallback: average face normals (cross products) into each vertex.
        print("  vtkPolyDataNormals returned nothing — using cross-product fallback")
        cells_flat = vtk_to_numpy(poly.GetPolys().GetData())
        tris       = cells_flat.reshape(-1, 4)[:, 1:]          # (T, 3)
        v0, v1, v2 = pts_np[tris[:, 0]], pts_np[tris[:, 1]], pts_np[tris[:, 2]]
        face_n     = np.cross(v1 - v0, v2 - v0).astype(float)
        lengths    = np.linalg.norm(face_n, axis=1, keepdims=True)
        lengths[lengths == 0] = 1.0
        face_n    /= lengths
        normals_np = np.zeros_like(pts_np)
        for i in range(3):
            np.add.at(normals_np, tris[:, i], face_n)
        lengths    = np.linalg.norm(normals_np, axis=1, keepdims=True)
        lengths[lengths == 0] = 1.0
        normals_np /= lengths

    # Outward-orientation check: for each vertex the outward direction is
    # (vertex - centroid). The dot product with the normal should be positive
    # if the normal faces outward. Average over all vertices and flip if needed.
    centroid = pts_np.mean(axis=0)
    outward  = pts_np - centroid
    dot_sum  = np.einsum('ij,ij->i', outward, normals_np).mean()

    if dot_sum < 0:
        print("  Normals were inward-facing — flipping to outward")
        normals_np = -normals_np
    else:
        print("  Normals are outward-facing ✓")

    return [(float(n[0]), float(n[1]), float(n[2])) for n in normals_np]


# ---------------------------------------------------------------------------
# Scalar field helpers
# ---------------------------------------------------------------------------

def list_fields(dataset):
    """Print all available point and cell data arrays."""
    print("\nAvailable fields:")
    pd = dataset.GetPointData()
    print("  point_data (per vertex):")
    for i in range(pd.GetNumberOfArrays()):
        arr = pd.GetArray(i)
        print(f"    {arr.GetName()}  "
              f"({arr.GetNumberOfTuples()} tuples, "
              f"{arr.GetNumberOfComponents()} components)")

    cd = dataset.GetCellData()
    print("  cell_data (per cell):")
    for i in range(cd.GetNumberOfArrays()):
        arr = cd.GetArray(i)
        print(f"    {arr.GetName()}  "
              f"({arr.GetNumberOfTuples()} tuples, "
              f"{arr.GetNumberOfComponents()} components)")


def get_scalar_field(dataset, poly, field_name=None):
    """
    Return a 1D numpy array of scalar values — one per vertex of `poly`.

    Searches point_data first (already per-vertex). Falls back to cell_data,
    which is propagated to points via vtkCellDataToPointData.

    Works with both vtkUnstructuredGrid (vtu) and vtkPolyData (vtp) datasets.
    If field_name is None, picks the first available scalar array.
    """
    def first_scalar(data_obj):
        """Return the first array name regardless of component count."""
        if data_obj.GetNumberOfArrays() > 0:
            return data_obj.GetArray(0).GetName()
        return None

    def to_scalar(arr):
        """
        Convert a VTK array to a 1D numpy float array.
        - 1-component arrays are returned as-is.
        - Multi-component arrays (vectors, tensors) return per-tuple magnitude.
        """
        data = vtk_to_numpy(arr).astype(float)
        if data.ndim == 1:
            return data
        # Vector/tensor field — compute magnitude
        mag = np.linalg.norm(data, axis=1)
        n_components = arr.GetNumberOfComponents()
        print(f"  Note: '{arr.GetName()}' has {n_components} components — "
              f"using magnitude.")
        return mag

    # --- Point data on the surface poly ---
    pd   = poly.GetPointData()
    name = field_name or first_scalar(pd)
    if name:
        arr = pd.GetArray(name)
        if arr:
            return to_scalar(arr), name, "point_data"

    # --- Cell data: propagate to points then remap to surface ---
    cd   = dataset.GetCellData()
    name = field_name or first_scalar(cd)
    if name:
        c2p = vtk.vtkCellDataToPointData()
        c2p.SetInputData(dataset)
        c2p.Update()

        # Re-extract the surface so point indices match our poly
        surface = vtk.vtkDataSetSurfaceFilter()
        surface.SetInputData(c2p.GetOutput())
        surface.Update()
        tri = vtk.vtkTriangleFilter()
        tri.SetInputConnection(surface.GetOutputPort())
        tri.Update()

        arr = tri.GetOutput().GetPointData().GetArray(name)
        if arr:
            return (to_scalar(arr),
                    name,
                    "cell_data (propagated to vertices)")

    raise ValueError(
        f"Scalar field {repr(field_name) if field_name else '(any)'} not found.\n"
        "Run with --list-fields to see what's available."
    )


# ---------------------------------------------------------------------------
# Color map helpers
# ---------------------------------------------------------------------------

COLORMAP_RESOLUTION = 256


def build_colormap_texture(colormap_name="viridis"):
    """
    Build a 1D color map texture (COLORMAP_RESOLUTION x 1) from a matplotlib
    colormap. Returns a make_color_map()-compatible dict.
    """
    cmap   = plt.get_cmap(colormap_name)
    t      = np.linspace(0, 1, COLORMAP_RESOLUTION)
    rgba   = cmap(t)
    colors = [(int(r * 255), int(g * 255), int(b * 255))
              for r, g, b, _ in rgba]
    return make_color_map(colors, width=COLORMAP_RESOLUTION, height=1)


def normalize_points(points, target_size_cm=200.0, center=None, scale=None):
    """
    Uniformly scale and center the mesh so its largest dimension equals
    target_size_cm (in Unreal centimetres).

    Args:
        points:         numpy array of shape (N, 3) in original file units.
        target_size_cm: largest bounding box extent after scaling, in cm.
                        Default 200 = 2 metres.
        center:         if provided together with scale, skip computing them
                        from this frame's data and use these values directly.
                        Use this to apply a consistent transform across all
                        frames of an animation so each frame occupies the same
                        region of space regardless of how the mesh changes.
        scale:          scalar multiplier to apply after centering.

    Returns a new (N, 3) numpy array in Unreal centimetres.
    """
    if center is None or scale is None:
        min_coords = points.min(axis=0)
        max_coords = points.max(axis=0)
        extents    = max_coords - min_coords
        max_extent = extents.max()

        if max_extent == 0:
            return points

        scale  = target_size_cm / max_extent
        center = (min_coords + max_coords) / 2.0

    return (points - center) * scale


def compute_animation_transform(all_points, target_size_cm=200.0):
    """
    Compute one center + scale that covers every frame of an animation.
    Uses the global bounding box so no single frame is ever re-centered.

    Args:
        all_points:     numpy array of shape (TotalPoints, 3) — stack all
                        frames' raw point arrays before calling.
        target_size_cm: same value passed to normalize_points().

    Returns (center, scale) — pass both to normalize_points() for each frame.
    """
    min_coords = all_points.min(axis=0)
    max_coords = all_points.max(axis=0)
    extents    = max_coords - min_coords
    max_extent = extents.max()

    if max_extent == 0:
        return all_points.mean(axis=0), 1.0

    scale  = target_size_cm / max_extent
    center = (min_coords + max_coords) / 2.0
    return center, scale


def scalar_to_uvs(scalar_values, vmin=None, vmax=None):
    """
    Normalise scalar values to [0, 1] using true min-max and return (U, V)
    pairs where U = normalised scalar, V = 0.5 (centre of the 1-pixel-tall
    texture).

    Args:
        vmin: explicit lower bound (default: data minimum)
        vmax: explicit upper bound (default: data maximum)
    """
    scalar_values = np.asarray(scalar_values, dtype=float).ravel()

    if vmin is None:
        vmin = scalar_values.min()
    if vmax is None:
        vmax = scalar_values.max()

    print(f"  Color range: {vmin:.4g} – {vmax:.4g}")

    if vmax == vmin:
        normalised = np.zeros_like(scalar_values)
    else:
        normalised = (scalar_values - vmin) / (vmax - vmin)

    normalised = np.clip(normalised, 0.0, 1.0)
    return [(float(u), 0.5) for u in normalised]


# ---------------------------------------------------------------------------
# Prepare geometry (done once)
# ---------------------------------------------------------------------------

def prepare_geometry(vtu_path, target_size=200.0, center=None, scale=None):
    """
    Load the file, extract the surface, triangulate, normalise, and compute normals.
    Returns (dataset, poly, verts, tris, normals) — everything needed to re-colour
    and re-send without reloading the file.

    If `center` and `scale` are provided they are used as-is instead of being
    derived from this file's own bounding box.  Pass them when all animation
    frames must share the same spatial reference (see compute_animation_transform).
    """
    print(f"Loading {vtu_path} ...")
    dataset, file_type = load_file(vtu_path)
    print(f"  {dataset.GetNumberOfPoints()} points, "
          f"{dataset.GetNumberOfCells()} cells  [{file_type.upper()}]")

    print("Extracting surface and triangulating ...")
    poly = extract_surface_triangles(dataset, file_type)
    print(f"  {poly.GetNumberOfPoints()} surface vertices, "
          f"{poly.GetNumberOfCells()} triangles")

    points, indices = polydata_to_arrays(poly)

    points = normalize_points(points, target_size, center=center, scale=scale)
    extents = points.max(axis=0) - points.min(axis=0)
    print(f"  Normalised extents: "
          f"{extents[0]:.1f} x {extents[1]:.1f} x {extents[2]:.1f} cm")

    verts = [(float(p[0]), float(p[1]), float(p[2])) for p in points]
    tris  = indices.flatten().tolist()

    print("Computing surface normals ...")
    normals = compute_normals(poly)
    print(f"  {len(normals)} vertex normals computed")

    return dataset, poly, verts, tris, normals


def send_with_field(dataset, poly, verts, tris, normals,
                    field_name, colormap_name, mesh_id, host, port,
                    volume_id=None):
    """
    Recolour an already-prepared mesh using the given scalar field and send it.
    """
    try:
        scalar_values, chosen, source = get_scalar_field(dataset, poly, field_name)
    except ValueError as e:
        print(f"  Error: {e}")
        return False

    print(f"  Field '{chosen}' from {source} — "
          f"range {scalar_values.min():.4g} – {scalar_values.max():.4g}")

    uvs  = scalar_to_uvs(scalar_values)
    cmap = build_colormap_texture(colormap_name)

    print(f"  Sending '{mesh_id}' to Unreal at {host}:{port} ...")
    send_mesh(verts, tris, uvs=uvs, normals=normals, color_map=cmap,
              mesh_id=mesh_id, volume_id=volume_id, host=host, port=port)
    print(f"  Done.")
    return True


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def load_and_send(vtu_path, field_name=None, colormap_name="viridis",
                  target_size=200.0, mesh_id=None, volume_id=None,
                  host="127.0.0.1", port=9000):
    """Single-shot: load, colour with one field, send."""
    dataset, poly, verts, tris, normals = prepare_geometry(vtu_path, target_size)
    send_with_field(dataset, poly, verts, tris, normals,
                    field_name, colormap_name, mesh_id, host, port,
                    volume_id=volume_id)


def interactive_mode(vtu_path, initial_field=None, initial_colormap="viridis",
                     target_size=200.0, mesh_id=None, volume_id=None,
                     host="127.0.0.1", port=9000):
    """
    Load the mesh once, then loop — prompting for a field and colormap each
    time and re-sending to Unreal immediately.

    Commands at the prompt:
        <field>              — recolour with that field, keep current colormap
        <field> <colormap>   — recolour with that field and a new colormap
        list                 — print available fields
        colormaps            — print a few useful matplotlib colormap names
        quit / exit / q      — exit
    """
    dataset, poly, verts, tris, normals = prepare_geometry(vtu_path, target_size)
    list_fields(dataset)

    current_colormap = initial_colormap

    # Send with initial field immediately
    if initial_field:
        send_with_field(dataset, poly, verts, tris, normals,
                        initial_field, current_colormap, mesh_id, host, port,
                        volume_id=volume_id)

    print("\nInteractive mode — type a field name to recolour and send.")
    print("  Commands: list, colormaps, quit\n")

    while True:
        try:
            raw = input("field [colormap] > ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nExiting.")
            break

        if not raw:
            continue

        if raw.lower() in ("quit", "exit", "q"):
            break

        if raw.lower() == "list":
            list_fields(dataset)
            continue

        if raw.lower() == "colormaps":
            print("  Some useful colormaps:")
            for name in ["viridis", "plasma", "inferno", "magma", "cividis",
                         "jet", "rainbow", "coolwarm", "RdBu", "seismic",
                         "hot", "cool", "turbo"]:
                print(f"    {name}")
            continue

        parts = raw.split()
        field   = parts[0]
        colormap = parts[1] if len(parts) > 1 else current_colormap

        # Validate colormap before sending
        try:
            plt.get_cmap(colormap)
        except ValueError:
            print(f"  Unknown colormap '{colormap}'. Type 'colormaps' for suggestions.")
            continue

        current_colormap = colormap
        send_with_field(dataset, poly, verts, tris, normals,
                        field, current_colormap, mesh_id, host, port,
                        volume_id=volume_id)


def send_animation(vtu_paths, field_name=None, colormap_name="viridis",
                   target_size=200.0, mesh_id=None, volume_id=None,
                   fps=24.0, host="127.0.0.1", port=9000):
    """
    Send a sequence of VTU/VTP files as an animation to Unreal.

    Uses a two-pass approach so every frame occupies the same position and
    scale in Unreal space:

      Pass 1 — load raw geometry for all frames, stack all points together,
               compute one global bounding-box center + scale and one global
               scalar min/max.
      Pass 2 — apply the same center/scale to each frame, compute normals,
               normalise scalar UVs against the global range, then send.

    This prevents the mesh from visually "jumping" or rescaling between
    frames when the contour changes size or position.

    The colormap texture is only sent with frame 0 — subsequent frames carry
    only geometry, normals, and UVs.
    """
    n        = len(vtu_paths)
    frame_id = mesh_id or "animation"
    cmap     = build_colormap_texture(colormap_name)

    # ------------------------------------------------------------------
    # Pass 1: load raw geometry + scalars, accumulate global bounds
    # ------------------------------------------------------------------
    print(f"Pass 1 — loading {n} frames to compute global bounds ...")
    raw_frames   = []   # (dataset, poly, raw_points, indices)
    all_pts_list = []
    all_scalars  = []

    for i, path in enumerate(vtu_paths):
        print(f"  [{i + 1}/{n}] {path}")
        dataset, file_type  = load_file(path)
        poly                = extract_surface_triangles(dataset, file_type)
        raw_points, indices = polydata_to_arrays(poly)

        if indices.shape[0] == 0:
            print(f"  [{i + 1}/{n}] WARNING: no triangles — frame will be skipped")
            raw_frames.append(None)
            continue

        all_pts_list.append(raw_points)
        raw_frames.append((dataset, poly, raw_points, indices))

        try:
            sv, _, _ = get_scalar_field(dataset, poly, field_name)
            all_scalars.append(sv)
        except ValueError:
            pass

    # Global spatial transform — one center + scale for all frames
    all_pts = np.vstack(all_pts_list)
    g_center, g_scale = compute_animation_transform(all_pts, target_size)
    g_extents = (all_pts.max(axis=0) - all_pts.min(axis=0)) * g_scale
    print(f"Global extents: "
          f"{g_extents[0]:.1f} x {g_extents[1]:.1f} x {g_extents[2]:.1f} cm")

    # Global scalar range — consistent colours across all frames
    if all_scalars:
        g_vmin = float(min(s.min() for s in all_scalars))
        g_vmax = float(max(s.max() for s in all_scalars))
        print(f"Global scalar range: {g_vmin:.4g} – {g_vmax:.4g}")
    else:
        g_vmin = g_vmax = None

    # ------------------------------------------------------------------
    # Pass 2: normalise, compute normals + UVs  (skip empty frames)
    # ------------------------------------------------------------------
    print(f"\nPass 2 — computing normals and UVs ...")
    frames = []   # only the sendable frames
    for i, entry in enumerate(raw_frames):
        if entry is None:
            continue   # no geometry this time step

        dataset, poly, raw_points, indices = entry
        print(f"  [{i + 1}/{n}] normals + UVs ...")
        points  = normalize_points(raw_points, target_size,
                                   center=g_center, scale=g_scale)
        verts   = [(float(p[0]), float(p[1]), float(p[2])) for p in points]
        tris    = indices.flatten().tolist()
        normals = compute_normals(poly)

        try:
            sv, chosen, _ = get_scalar_field(dataset, poly, field_name)
            uvs = scalar_to_uvs(sv, vmin=g_vmin, vmax=g_vmax)
            print(f"    Field '{chosen}'")
        except ValueError as e:
            print(f"    Warning: {e} — no colour")
            uvs = None

        frames.append((verts, tris, normals, uvs))

    # ------------------------------------------------------------------
    # Send
    # ------------------------------------------------------------------
    n_send = len(frames)
    if n_send == 0:
        print("WARNING: All frames were empty — nothing sent.")
        return

    skipped = n - n_send
    if skipped:
        print(f"NOTE: {skipped} of {n} frames had no geometry and were skipped.")

    print(f"\nSending {n_send} frames → {host}:{port}  (fps={fps}) ...")
    t0 = time.time()
    for i, (verts, tris, normals, uvs) in enumerate(frames):
        send_mesh(verts, tris, normals=normals, uvs=uvs,
                  color_map=(cmap if i == 0 else None),
                  mesh_id=frame_id, volume_id=volume_id,
                  frame_index=i, total_frames=n_send,
                  playback_fps=fps, host=host, port=port)
        print(f"  [{i + 1}/{n_send}] frame {i} sent")

    print(f"All {n_send} frames sent in {time.time() - t0:.1f}s")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Send VTU/VTP file(s) to Unreal Engine. "
                    "Pass multiple files to send an animation.")
    parser.add_argument("vtu", nargs="+",
        help="One or more .vtu/.vtp files. Multiple files = animation sequence.")
    parser.add_argument("--field", default=None,
        help="Scalar field to use for coloring (default: first available)")
    parser.add_argument("--colormap", default="viridis",
        help="Matplotlib colormap name (default: viridis)")
    parser.add_argument("--size", default=200.0, type=float,
        help="Target size in Unreal cm for the largest dimension (default: 200)")
    parser.add_argument("--id", default=None, dest="mesh_id",
        help="Mesh identifier in Unreal (default: filename stem for single file, "
             "'animation' for multiple)")
    parser.add_argument("--volume", default=None, dest="volume_id",
        help="VolumeId of the ADisplayVolumeActor in Unreal to display this mesh in "
             "(default: 'default')")
    parser.add_argument("--fps", default=24.0, type=float,
        help="Animation playback frame rate (default: 24, ignored for single file)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=9000, type=int)
    parser.add_argument("--list-fields", action="store_true",
        help="List all fields in the first VTU file and exit")
    parser.add_argument("--interactive", "-i", action="store_true",
        help="Interactive mode — prompt to change field/colormap without reloading "
             "(single file only)")
    args = parser.parse_args()

    if args.list_fields:
        dataset_check, _ = load_file(args.vtu[0])
        list_fields(dataset_check)
        sys.exit(0)

    if len(args.vtu) > 1:
        # Animation mode
        mesh_id = args.mesh_id or "animation"
        print(f"Animation mode: {len(args.vtu)} frames, mesh ID '{mesh_id}'")
        send_animation(
            vtu_paths     = args.vtu,
            field_name    = args.field,
            colormap_name = args.colormap,
            target_size   = args.size,
            mesh_id       = mesh_id,
            volume_id     = args.volume_id,
            fps           = args.fps,
            host          = args.host,
            port          = args.port,
        )
    elif args.interactive:
        mesh_id = args.mesh_id or args.vtu[0].rsplit("/", 1)[-1].rsplit("\\", 1)[-1].rsplit(".", 1)[0]
        print(f"Mesh ID: '{mesh_id}'")
        interactive_mode(
            vtu_path         = args.vtu[0],
            initial_field    = args.field,
            initial_colormap = args.colormap,
            target_size      = args.size,
            mesh_id          = mesh_id,
            volume_id        = args.volume_id,
            host             = args.host,
            port             = args.port,
        )
    else:
        mesh_id = args.mesh_id or args.vtu[0].rsplit("/", 1)[-1].rsplit("\\", 1)[-1].rsplit(".", 1)[0]
        print(f"Mesh ID: '{mesh_id}'")
        load_and_send(
            vtu_path      = args.vtu[0],
            field_name    = args.field,
            colormap_name = args.colormap,
            target_size   = args.size,
            mesh_id       = mesh_id,
            volume_id     = args.volume_id,
            host          = args.host,
            port          = args.port,
        )
