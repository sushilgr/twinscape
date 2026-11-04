"""Builds a synthetic 'google3d' area offline (no key, no network) so the HDA / Houdini Engine /
Unreal side of the Photorealistic 3D Tiles mode can be exercised end to end.

The fake tiles are real Draco glbs (see test_google3d.make_tile) of a ground slab + a few towers,
pushed through the same decode -> atlas -> bgeo path as live tiles."""
import numpy as np

from earthtwin import geo, google3d, ingest
from earthtwin.tests.test_google3d import make_tile

BOX_F = np.array([[0, 1, 3], [0, 3, 2], [4, 6, 7], [4, 7, 5], [0, 4, 5], [0, 5, 1],
                  [2, 3, 7], [2, 7, 6], [0, 2, 6], [0, 6, 4], [1, 5, 7], [1, 7, 3]])


def _box(e, n, w, d, h, z0=0.0):
    pts = np.array([[x, y, z] for z in (z0, z0 + h) for y in (n - d / 2, n + d / 2) for x in (e - w / 2, e + w / 2)], float)
    uv = np.c_[(pts[:, 0] - pts[:, 0].min()) / max(1, w), (pts[:, 2] - z0) / max(1, h)]
    return pts, uv


def fake_fetch(frame, size, api_key, out_dir, max_error=3.0, progress=None, meta=None):
    rng = np.random.default_rng(7)
    ell_h = -32.0                                      # NYC geoid offset: ellipsoid height of the ground
    tiles = []
    half = size * 0.5
    g = np.array([[-half - 50, -half - 50, ell_h], [half + 50, -half - 50, ell_h],
                  [-half - 50, half + 50, ell_h], [half + 50, half + 50, ell_h]])
    tiles.append((g, np.array([[0, 1, 3], [0, 3, 2]]), np.c_[[0, 1, 0, 1], [0, 0, 1, 1]].astype(float)))
    for _ in range(40):
        e, n = rng.uniform(-half * 0.9, half * 0.9, 2)
        p, uv = _box(e, n, rng.uniform(15, 45), rng.uniform(15, 45), rng.uniform(15, 250), ell_h)
        tiles.append((p, BOX_F, uv))
    parts = []
    for p, f, uv in tiles:
        rtc = frame.enu_to_ecef(p.mean(axis=0, keepdims=True))[0]
        glb = make_tile(frame, p, f, uv, rtc, np.zeros(3), np.eye(4))
        parts.extend(google3d.decode_glb(glb, frame, np.eye(4)))
    if meta is not None:
        meta["g3d_tiles"] = len(tiles)
    return parts, google3d.build_atlas(parts, out_dir)


def run(lat=40.708, lon=-74.0115, size=1200.0):
    real = google3d.fetch
    google3d.fetch = fake_fetch
    try:
        return ingest.fetch(dict(name="G3D Offline Test", lat=lat, lon=lon, size=size, mode="google3d"),
                            force=True)
    finally:
        google3d.fetch = real
