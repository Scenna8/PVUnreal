"""
sender.py

Sends mesh data to a running Unreal Engine instance over TCP.
Unreal must have the MeshSocketSubsystem active, listening on port 9000.

Protocol:
  - Every message is prefixed with a 4-byte big-endian unsigned integer
    containing the byte length of the JSON payload that follows.
  - The JSON payload has the shape:
      {
        "verts":    [{"X": float, "Y": float, "Z": float}, ...],
        "tris":     [int, int, int, ...],
        "uvs":      [{"U": float, "V": float}, ...],   # optional, one per vertex
        "colormap": {                                   # optional
            "w":    int,
            "h":    int,
            "data": [int, ...]   # flat RGBA8 (0-255), row-major, top-left origin
        }
      }
  - Coordinates are in Unreal units (centimetres).
  - UV (0,0) = top-left of the color map, (1,1) = bottom-right.
"""

import socket
import struct
import json


def triangles_to_mesh(triangles):
    """
    Convert a list of (verts, uvs) triangle tuples into the flat
    (vertices, indices, uvs) format expected by send_mesh().

    Each entry must be:
        (
            ((x0,y0,z0), (x1,y1,z1), (x2,y2,z2)),   # 3D positions
            ((u0,v0),    (u1,v1),    (u2,v2))         # UV coordinates
        )

    Vertices are NOT deduplicated so that each vertex can have a unique UV.

    Returns:
        vertices — list of (x, y, z) tuples
        indices  — flat list of ints
        uvs      — list of (u, v) tuples, one per vertex
    """
    vertices = []
    indices  = []
    uvs      = []

    for tri_verts, tri_uvs in triangles:
        base = len(vertices)
        for point, uv in zip(tri_verts, tri_uvs):
            vertices.append(tuple(point))
            uvs.append(tuple(uv))
        indices.extend([base, base + 1, base + 2])

    return vertices, indices, uvs


def make_color_map(colors, width, height):
    """
    Build a color map dict ready to pass to send_mesh().

    Args:
        colors: list of (r, g, b) tuples with values 0-255, row-major,
                top-left first. Must have exactly width * height entries.
        width:  number of columns.
        height: number of rows.

    Returns a dict with keys "w", "h", and "data" (flat RGBA8 list).
    """
    assert len(colors) == width * height, \
        f"Expected {width * height} colors, got {len(colors)}"

    flat = []
    for r, g, b in colors:
        flat.extend([int(r), int(g), int(b), 255])   # append alpha = 255

    return {"w": width, "h": height, "data": flat}


def send_mesh(vertices, triangles, uvs=None, normals=None, color_map=None,
              mesh_id=None, volume_id=None, frame_index=None, total_frames=None,
              playback_fps=None, anim_bounds_min=None, anim_bounds_max=None,
              host="127.0.0.1", port=9000):
    """
    Send a mesh update to Unreal.

    Args:
        vertices:         list of (x, y, z) tuples.
        anim_bounds_min:  optional (x, y, z) — global min across all animation
                          frames in source units.  Unreal uses these together with
                          anim_bounds_max to compute one consistent normalization
                          transform for the whole sequence so the bounding box
                          doesn't shift between frames.
        anim_bounds_max:  optional (x, y, z) — global max across all animation
                          frames in source units.
        triangles:     flat list of ints — triangle indices.
        uvs:           optional list of (u, v) tuples, one per vertex.
        normals:       optional list of (nx, ny, nz) tuples, one per vertex.
                       If omitted, Unreal auto-calculates normals from geometry.
        color_map:     optional dict from make_color_map().
        mesh_id:       optional string identifier. Unreal uses this to route the
                       payload to the correct mesh actor. Omit to use the default.
        volume_id:     optional string identifying which ADisplayVolumeActor in the
                       level to display this mesh in. Must match the VolumeId set on
                       the actor in the Unreal Details panel. Omit to use "default".
        frame_index:   int >= 0 to mark this as an animation frame.
                       Unreal buffers frames by index and starts looped playback
                       once all expected frames arrive.
        total_frames:  total number of frames in the animation. Unreal uses this
                       to know when all frames have been received.
        playback_fps:  target playback frame rate (default 24 if omitted).
        host:          IP of the machine running Unreal.
        port:          TCP port Unreal is listening on.
    """
    payload_dict = {
        "verts": [{"X": v[0], "Y": v[1], "Z": v[2]} for v in vertices],
        "tris":  triangles,
    }

    if mesh_id is not None:
        payload_dict["id"] = mesh_id

    if volume_id is not None:
        payload_dict["volume_id"] = volume_id

    if frame_index is not None:
        payload_dict["frame"] = frame_index
    if total_frames is not None:
        payload_dict["total_frames"] = total_frames
    if playback_fps is not None:
        payload_dict["fps"] = playback_fps

    if uvs:
        payload_dict["uvs"] = [{"U": uv[0], "V": uv[1]} for uv in uvs]

    if normals:
        payload_dict["normals"] = [{"X": n[0], "Y": n[1], "Z": n[2]} for n in normals]

    if color_map:
        payload_dict["colormap"] = color_map

    if anim_bounds_min is not None:
        payload_dict["anim_bounds_min"] = {
            "X": float(anim_bounds_min[0]),
            "Y": float(anim_bounds_min[1]),
            "Z": float(anim_bounds_min[2]),
        }
    if anim_bounds_max is not None:
        payload_dict["anim_bounds_max"] = {
            "X": float(anim_bounds_max[0]),
            "Y": float(anim_bounds_max[1]),
            "Z": float(anim_bounds_max[2]),
        }

    payload = json.dumps(payload_dict).encode("utf-8")
    header  = struct.pack(">I", len(payload))

    with socket.create_connection((host, port)) as sock:
        sock.sendall(header + payload)


# =============================================================================
# Example — a square with a 2x2 color map (red, green, blue, yellow corners)
# =============================================================================

if __name__ == "__main__":
    Z = 100   # height off the ground (cm)

    # Each triangle: (3D positions, UV coordinates)
    # UV (0,0) = top-left of the color map
    # UV (1,1) = bottom-right of the color map
    triangle_list = [
        # Triangle 1
        (((  0,   0, Z), (100,   0, Z), (100, 100, Z)),
         ((  0,   1),    (  1,   1),    (  1,   0))),

        # Triangle 2
        (((  0,   0, Z), (100, 100, Z), (  0, 100, Z)),
         ((  0,   1),    (  1,   0),    (  0,   0))),
    ]

    # 2x2 color map — top-left=red, top-right=green, bottom-left=blue, bottom-right=yellow
    cmap = make_color_map(
        colors=[
            (255,   0,   0),   # top-left     → red
            (  0, 255,   0),   # top-right    → green
            (  0,   0, 255),   # bottom-left  → blue
            (255, 255,   0),   # bottom-right → yellow
        ],
        width=2, height=2
    )

    verts, tris, uvs = triangles_to_mesh(triangle_list)
    send_mesh(verts, tris, uvs=uvs, color_map=cmap)
    print(f"Sent {len(triangle_list)} triangles, {len(verts)} vertices, "
          f"{cmap['w']}x{cmap['h']} color map.")
