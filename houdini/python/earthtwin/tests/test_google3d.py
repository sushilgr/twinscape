"""Offline check of the Google 3D Tiles decoder: builds a Draco-compressed, textured glb the way
Google serves them (glTF Y-up, CESIUM_RTC centre, node matrix, tile transform) and verifies the
decoded points land back on known local ENU positions."""
import io
import json
import struct

import numpy as np
from PIL import Image

from earthtwin import geo, google3d


def _glb(json_doc, bin_chunk):
    js = json.dumps(json_doc).encode("utf-8")
    js += b" " * ((4 - len(js) % 4) % 4)
    bin_chunk += b"\0" * ((4 - len(bin_chunk) % 4) % 4)
    total = 12 + 8 + len(js) + 8 + len(bin_chunk)
    return (struct.pack("<4sII", b"glTF", 2, total) + struct.pack("<II", len(js), 0x4E4F534A) + js
            + struct.pack("<II", len(bin_chunk), 0x004E4942) + bin_chunk)


def make_tile(frame, enu_pts, faces, uv, rtc, node_t, tile_m):
    """enu_pts -> ECEF -> undo tile transform -> undo RTC -> undo node translation -> to glTF Y-up."""
    import DracoPy
    ecef = frame.enu_to_ecef(enu_pts)
    inv_tile = np.linalg.inv(tile_m)
    local = (np.c_[ecef - 0, np.ones(len(ecef))] @ inv_tile.T)[:, :3]   # tile space (Z-up)
    local = local - rtc - node_t                                         # remove RTC + node translation
    yup = np.c_[local[:, 0], local[:, 2], -local[:, 1]]                  # Z-up -> glTF Y-up
    enc = DracoPy.encode(yup.astype(np.float32), faces.astype(np.uint32), quantization_bits=16,
                         tex_coord=uv.astype(np.float64))
    jpg = io.BytesIO()
    Image.fromarray((np.random.default_rng(1).random((64, 64, 3)) * 255).astype(np.uint8)).save(jpg, "JPEG")
    jpg = jpg.getvalue()
    pad = (4 - len(enc) % 4) % 4
    binc = enc + b"\0" * pad + jpg
    nt = (np.c_[[node_t[0]], [node_t[2]], [-node_t[1]]])[0]            # node translation in Y-up space
    doc = {
        "asset": {"version": "2.0"},
        "extensionsUsed": ["KHR_draco_mesh_compression", "CESIUM_RTC"],
        "extensions": {"CESIUM_RTC": {"center": list(map(float, rtc))}},
        "scene": 0, "scenes": [{"nodes": [0]}],
        "nodes": [{"mesh": 0, "translation": list(map(float, nt))}],
        "meshes": [{"primitives": [{"attributes": {"POSITION": 0, "TEXCOORD_0": 1}, "indices": 2, "material": 0,
                                    "extensions": {"KHR_draco_mesh_compression": {"bufferView": 0,
                                                   "attributes": {"POSITION": 0, "TEXCOORD_0": 1}}}}]}],
        "accessors": [{"componentType": 5126, "count": len(enu_pts), "type": "VEC3"},
                      {"componentType": 5126, "count": len(enu_pts), "type": "VEC2"},
                      {"componentType": 5125, "count": faces.size, "type": "SCALAR"}],
        "materials": [{"pbrMetallicRoughness": {"baseColorTexture": {"index": 0}}}],
        "textures": [{"source": 0}],
        "images": [{"bufferView": 1, "mimeType": "image/jpeg"}],
        "bufferViews": [{"buffer": 0, "byteOffset": 0, "byteLength": len(enc)},
                        {"buffer": 0, "byteOffset": len(enc) + pad, "byteLength": len(jpg)}],
        "buffers": [{"byteLength": len(binc)}],
    }
    return _glb(doc, binc)


def run(out_dir="/tmp"):
    frame = geo.LocalFrame(40.708, -74.0115, 0.0)
    # a 40 m box standing 0..120 m tall, 300 m east / 200 m north of the origin
    e0, n0 = 300.0, 200.0
    corners = np.array([[x, y, z] for z in (0, 120) for y in (n0 - 20, n0 + 20) for x in (e0 - 20, e0 + 20)], float)
    faces = np.array([[0, 1, 3], [0, 3, 2], [4, 6, 7], [4, 7, 5], [0, 4, 5], [0, 5, 1],
                      [2, 3, 7], [2, 7, 6], [0, 2, 6], [0, 6, 4], [1, 5, 7], [1, 7, 3]])
    uv = np.c_[(corners[:, 0] - e0 + 20) / 40, (corners[:, 2]) / 120]
    rtc = frame.origin + np.array([100.0, -50.0, 25.0])
    node_t = np.array([3.0, -2.0, 1.5])
    tile_m = np.eye(4)
    tile_m[:3, 3] = [-7.0, 4.0, 2.0]
    glb = make_tile(frame, corners, faces, uv, rtc, node_t, tile_m)
    parts = google3d.decode_glb(glb, frame, tile_m)
    P = parts[0]["P"]
    expect = frame.enu_to_houdini(corners)
    err = float(np.abs(np.sort(P, axis=0) - np.sort(expect, axis=0)).max())
    paths = google3d.build_atlas(parts, out_dir)
    return dict(points=len(P), tris=len(parts[0]["tri"]), max_err_m=err, has_tex=parts[0]["tex"] is not None,
                atlas=paths, uv_range=[float(parts[0]["uv_atlas"].min()), float(parts[0]["uv_atlas"].max())])


if __name__ == "__main__":
    print(run())
