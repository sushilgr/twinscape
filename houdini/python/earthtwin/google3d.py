"""Google Photorealistic 3D Tiles: tileset traversal, glb decode, texture atlas.

Requires an API key with the Map Tiles API enabled. Draco-compressed meshes are
decoded with DracoPy (installed on demand into Houdini's Python).

Usage terms: Google's Map Tiles API policies restrict pre-fetching, caching,
offline use and derived/extracted geometry. Treat this path as look-dev /
previs inside your own Google agreement; use the open-data path for assets you
ship.
"""
import io
import json
import math
import os
import struct
import subprocess
import sys
import threading
from urllib.parse import urljoin, urlparse, parse_qs

import numpy as np
from PIL import Image

from . import geo, net

ROOT = "https://tile.googleapis.com/v1/3dtiles/root.json"
BASE = "https://tile.googleapis.com"
YUP_TO_ZUP = np.array([[1, 0, 0, 0], [0, 0, -1, 0], [0, 1, 0, 0], [0, 0, 0, 1]], np.float64)


# ------------------------------------------------------------------ deps
def ensure_dracopy():
    try:
        import DracoPy  # noqa
        return True
    except ImportError:
        pass
    hfs = os.environ.get("HFS", "")
    cands = [os.path.join(hfs, "bin", "hython"), os.path.join(hfs, "bin", "python3"), sys.executable]
    for exe in cands:
        if exe and os.path.exists(exe):
            try:
                subprocess.check_call([exe, "-m", "pip", "install", "--user", "DracoPy"],
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=600)
                import site, importlib
                importlib.invalidate_caches()
                if site.getusersitepackages() not in sys.path:
                    sys.path.append(site.getusersitepackages())
                import DracoPy  # noqa
                return True
            except Exception:
                continue
    return False


# ------------------------------------------------------------------ traversal
def _obb_hits_sphere(box, c, r):
    ctr = np.array(box[0:3], np.float64)
    axes = [np.array(box[3:6]), np.array(box[6:9]), np.array(box[9:12])]
    d = c - ctr
    closest = ctr.copy()
    for a in axes:
        L = np.linalg.norm(a)
        if L < 1e-9:
            continue
        u = a / L
        t = np.clip(np.dot(d, u), -L, L)
        closest += t * u
    return np.linalg.norm(c - closest) <= r


def _tile_matrix(node, parent):
    t = node.get("transform")
    if not t:
        return parent
    return parent @ np.array(t, np.float64).reshape(4, 4).T   # 3D Tiles stores column-major


def _xform_box(box, m):
    if m is None or np.allclose(m, np.eye(4)):
        return box
    c = (m @ np.r_[box[0:3], 1.0])[:3]
    r = m[:3, :3]
    return list(c) + list(r @ np.array(box[3:6])) + list(r @ np.array(box[6:9])) + list(r @ np.array(box[9:12]))


def _footprint(box, frame):
    """ENU (east, north) min/max of the tile's oriented box."""
    c = np.array(box[0:3], np.float64)
    ax = [np.array(box[3:6]), np.array(box[6:9]), np.array(box[9:12])]
    corners = np.array([c + sx * ax[0] + sy * ax[1] + sz * ax[2]
                        for sx in (-1, 1) for sy in (-1, 1) for sz in (-1, 1)])
    enu = frame.ecef_to_enu(corners)
    return enu[:, :2].min(axis=0), enu[:, :2].max(axis=0)


def _obb_hits_square(box, frame, half):
    """Project the tile's oriented box into the local ENU frame and test its footprint against
    the capture square (tighter than the bounding-sphere test; ignores height)."""
    lo, hi = _footprint(box, frame)
    return lo[0] <= half and hi[0] >= -half and lo[1] <= half and hi[1] >= -half


def lod_error(dist, coarse, fine=0.0, radius=150.0):
    """Error threshold for a tile whose footprint is `dist` metres from the focus point.
    Inside `radius` tiles refine down to `fine`; beyond it the allowed error grows linearly
    with distance (constant screen-space error for a viewer standing at the focus) and is
    capped at `coarse`. fine <= 0 disables focus refinement (uniform `coarse`)."""
    if not fine or fine <= 0 or fine >= coarse:
        return coarse
    return min(coarse, fine * max(1.0, dist / max(1.0, radius)))


def _focus_dist(box, frame, focus):
    lo, hi = _footprint(box, frame)
    dx = max(lo[0] - focus[0], 0.0, focus[0] - hi[0])
    dy = max(lo[1] - focus[1], 0.0, focus[1] - hi[1])
    return math.hypot(dx, dy)


def traverse(frame, size, api_key, max_error=3.0, progress=None, max_tiles=4000,
             focus_error=0.0, focus_radius=150.0, focus=(0.0, 0.0), stats=None, hole=0.0):
    """Breadth-first, parallel traversal. Returns ([(glb_url, tile_matrix4x4)], session).

    With focus_error > 0 the traversal is view-dependent like a game engine's 3D Tiles
    streamer: tiles near `focus` (ENU metres from the area centre) refine to focus_error,
    farther tiles stop at progressively coarser levels, up to max_error at the edges.
    `stats` (dict) receives the selected tiles' geometric errors and focus distances.
    hole > 0 skips tiles lying entirely inside a centred square of that size (context rings
    around an already-fetched detailed area)."""
    hh = hole * 0.5 - 2.0
    centre = frame.origin
    radius = size * 0.7071 + 30.0
    state = {"session": None}
    lock = threading.Lock()

    def url_for(uri, base):
        u = urljoin(base, uri)
        q = parse_qs(urlparse(u).query)
        if "session" in q:
            with lock:
                state["session"] = q["session"][0]
        return u

    def fetch_json(u):
        params = {"key": api_key}
        if state["session"] and "session=" not in u:
            params["session"] = state["session"]
        return net.get(u, params=params, timeout=60).json()

    if progress:
        progress(0.02, "Google 3D Tiles: reading tileset")
    root = fetch_json(ROOT)
    frontier = [(root["root"], ROOT, np.eye(4))]
    leaves, level, fetched = [], 0, 1
    while frontier:
        level += 1
        nxt, subs = [], []
        for node, base, parent in frontier:
            m = _tile_matrix(node, parent)
            bv = node.get("boundingVolume", {})
            dist = 0.0
            if "box" in bv:
                box = _xform_box(bv["box"], m)
                if not _obb_hits_sphere(box, centre, radius):
                    continue
                if np.linalg.norm(box[3:6]) < 2e5:
                    if not _obb_hits_square(box, frame, size * 0.5 + 10.0):
                        continue
                    if hole > 0:
                        lo, hi = _footprint(box, frame)
                        if lo[0] > -hh and hi[0] < hh and lo[1] > -hh and hi[1] < hh:
                            continue
                    if focus_error:
                        dist = _focus_dist(box, frame, focus)
            thr = lod_error(dist, max_error, focus_error, focus_radius)
            content = node.get("content") or {}
            uri = content.get("uri") or content.get("url")
            kids = node.get("children") or []
            ge = float(node.get("geometricError", 0))
            if uri and uri.split("?")[0].endswith(".json"):
                subs.append((url_for(uri, base), m))
            elif kids and ge > thr:
                nxt.extend((k, base, m) for k in kids)
            elif uri and uri.split("?")[0].endswith(".glb"):
                leaves.append((url_for(uri, base), m))
                if stats is not None:
                    stats.setdefault("tiles", []).append((ge, dist))
            else:
                nxt.extend((k, base, m) for k in kids)
        if subs:
            docs = net.parallel(lambda sm: fetch_json(sm[0]), subs, workers=12)
            fetched += len(subs)
            nxt.extend((d["root"], u, m) for d, (u, m) in zip(docs, subs))
        if len(leaves) > max_tiles:
            raise net.FetchError("%d+ tiles selected - raise the error thresholds, shrink the focus "
                                 "radius or the area" % len(leaves))
        if progress:
            progress(min(0.19, 0.02 + 0.01 * level), "3D Tiles: level %d, %d tiles, %d tilesets read"
                     % (level, len(leaves), fetched))
        frontier = nxt
    seen, out = set(), []
    for u, m in leaves:
        k = u.split("?")[0]
        if k not in seen:
            seen.add(k)
            out.append((u, m))
    return out, state["session"]


# ------------------------------------------------------------------ glb
_CT = {5120: np.int8, 5121: np.uint8, 5122: np.int16, 5123: np.uint16, 5125: np.uint32, 5126: np.float32}
_NC = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4, "MAT4": 16}


def _parse_glb(b):
    magic, ver, length = struct.unpack("<4sII", b[:12])
    if magic != b"glTF":
        raise ValueError("not a glb")
    off, js, binc = 12, None, b""
    while off < length:
        clen, ctype = struct.unpack("<II", b[off:off + 8])
        data = b[off + 8:off + 8 + clen]
        if ctype == 0x4E4F534A:
            js = json.loads(data.decode("utf-8"))
        elif ctype == 0x004E4942:
            binc = data
        off += 8 + clen
    return js, binc


def _bv_bytes(js, binc, i):
    bv = js["bufferViews"][i]
    o = bv.get("byteOffset", 0)
    return binc[o:o + bv["byteLength"]], bv.get("byteStride")


def _accessor(js, binc, i):
    acc = js["accessors"][i]
    dt = _CT[acc["componentType"]]
    nc = _NC[acc["type"]]
    raw, stride = _bv_bytes(js, binc, acc["bufferView"])
    o = acc.get("byteOffset", 0)
    item = np.dtype(dt).itemsize * nc
    cnt = acc["count"]
    if stride and stride != item:
        a = np.lib.stride_tricks.as_strided(np.frombuffer(raw, np.uint8, offset=o),
                                            shape=(cnt, item), strides=(stride, 1))
        arr = np.frombuffer(a.copy().tobytes(), dt).reshape(cnt, nc)
    else:
        arr = np.frombuffer(raw, dt, count=cnt * nc, offset=o).reshape(cnt, nc)
    arr = arr.astype(np.float64)
    if acc.get("normalized"):
        arr /= float(np.iinfo(dt).max)
    return arr


def _node_mats(js):
    mats = {}

    def local(n):
        if "matrix" in n:
            return np.array(n["matrix"], np.float64).reshape(4, 4).T
        m = np.eye(4)
        if "scale" in n:
            m = np.diag(list(n["scale"]) + [1.0]) @ m
        if "rotation" in n:
            x, y, z, w = n["rotation"]
            r = np.array([[1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w), 0],
                          [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w), 0],
                          [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y), 0],
                          [0, 0, 0, 1]])
            m = r @ m
        if "translation" in n:
            t = np.eye(4)
            t[:3, 3] = n["translation"]
            m = t @ m
        return m

    def rec(i, parent):
        n = js["nodes"][i]
        w = parent @ local(n)
        mats.setdefault(i, w)
        for c in n.get("children", []):
            rec(c, w)

    scene = js.get("scenes", [{"nodes": list(range(len(js.get("nodes", []))))}])[js.get("scene", 0)]
    for i in scene.get("nodes", []):
        rec(i, np.eye(4))
    return mats


def decode_glb(data, frame, tile_matrix=None):
    """-> list of dict(P (n,3) houdini local, uv (n,2) gltf-space, tri (m,3), tex bytes)."""
    js, binc = _parse_glb(data)
    rtc = np.zeros(3)
    ext = js.get("extensions", {}).get("CESIUM_RTC")
    if ext:
        rtc = np.array(ext["center"], np.float64)
    parts = []
    for ni, world in _node_mats(js).items():
        node = js["nodes"][ni]
        if "mesh" not in node:
            continue
        for prim in js["meshes"][node["mesh"]]["primitives"]:
            dr = prim.get("extensions", {}).get("KHR_draco_mesh_compression")
            if dr:
                import DracoPy
                raw, _ = _bv_bytes(js, binc, dr["bufferView"])
                mesh = DracoPy.decode(bytes(raw))
                P = np.asarray(mesh.points, np.float64).reshape(-1, 3)
                tri = np.asarray(mesh.faces, np.int64).reshape(-1, 3)
                uv = np.asarray(mesh.tex_coord, np.float64).reshape(-1, 2) if getattr(mesh, "tex_coord", None) is not None and len(mesh.tex_coord) else np.zeros((len(P), 2))
            else:
                at = prim["attributes"]
                P = _accessor(js, binc, at["POSITION"])
                uv = _accessor(js, binc, at["TEXCOORD_0"]) if "TEXCOORD_0" in at else np.zeros((len(P), 2))
                tri = _accessor(js, binc, prim["indices"]).astype(np.int64).reshape(-1, 3) if "indices" in prim \
                    else np.arange(len(P)).reshape(-1, 3)
            tex = None
            mi = prim.get("material")
            if mi is not None:
                pbr = js["materials"][mi].get("pbrMetallicRoughness", {})
                bct = pbr.get("baseColorTexture")
                if bct is not None:
                    tt = bct.get("extensions", {}).get("KHR_texture_transform")
                    if tt:
                        uv = uv * np.array(tt.get("scale", [1, 1])) + np.array(tt.get("offset", [0, 0]))
                    img = js["images"][js["textures"][bct["index"]]["source"]]
                    if "bufferView" in img:
                        tex = bytes(_bv_bytes(js, binc, img["bufferView"])[0])
            # Cesium order: tile transform * RTC * Y-up->Z-up * node transform
            loc = (np.c_[P, np.ones(len(P))] @ (YUP_TO_ZUP @ world).T)[:, :3] + rtc
            if tile_matrix is not None and not np.allclose(tile_matrix, np.eye(4)):
                loc = (np.c_[loc, np.ones(len(loc))] @ tile_matrix.T)[:, :3]
            ecef = loc
            enu = frame.ecef_to_enu(ecef)
            parts.append({"P": frame.enu_to_houdini(enu).astype(np.float32), "uv": uv, "tri": tri, "tex": tex})
    return parts


def cut_hole(part, h):
    """Drop triangles whose centroid lies inside the centred square |x|,|z| < h (Houdini
    coords) and compact the vertex arrays. Returns None when nothing is left."""
    P, tri = part["P"], part["tri"]
    c = P[tri].mean(axis=1)
    keep = ~((np.abs(c[:, 0]) < h) & (np.abs(c[:, 2]) < h))
    if keep.all():
        return part
    if not keep.any():
        return None
    tri = tri[keep]
    used, inv = np.unique(tri.ravel(), return_inverse=True)
    part = dict(part, P=P[used], uv=part["uv"][used], tri=inv.reshape(-1, 3))
    return part


# ------------------------------------------------------------------ atlas
def build_atlas(parts, out_dir, page_px=8192, gutter=8, max_scale=1.0):
    """Pack every tile texture at its NATIVE resolution (Google serves 256..1024 px, often
    non-square) into 8K pages with shelf packing; UVs are remapped into the page. Nothing is
    resampled unless a single texture is larger than a page. An 8 px edge-extended gutter
    keeps neighbouring tiles from bleeding into each other down to the 1/8 mip."""
    rects = []
    for i, part in enumerate(parts):
        if part["tex"]:
            w, h = Image.open(io.BytesIO(part["tex"])).size
        else:
            w = h = 4
        w, h = int(w * max_scale), int(h * max_scale)
        w, h = min(w, page_px - 2 * gutter), min(h, page_px - 2 * gutter)
        rects.append([i, w, h])
    # shelf packing, tallest first
    order = sorted(rects, key=lambda r: (-r[2], -r[1]))
    place = {}
    page, x, y, shelf_h = 0, 0, 0, 0
    used = [[0, 0]]
    for i, w, h in order:
        W, H = w + 2 * gutter, h + 2 * gutter
        if x + W > page_px:
            x, y, shelf_h = 0, y + shelf_h, 0
        if y + H > page_px:
            page, x, y, shelf_h = page + 1, 0, 0, 0
            used.append([0, 0])
        place[i] = (page, x, y, w, h)
        x += W
        shelf_h = max(shelf_h, H)
        used[page][0] = max(used[page][0], x)
        used[page][1] = max(used[page][1], y + H)
    sides = [min(page_px, 1 << int(math.ceil(math.log2(max(64, max(u)))))) for u in used]
    paths = []
    for pg in range(len(sides)):
        side = sides[pg]
        img = np.full((side, side, 3), 127, np.uint8)
        for i, part in enumerate(parts):
            p_, x0, y0, w, h = place[i]
            if p_ != pg:
                continue
            if part["tex"]:
                im = Image.open(io.BytesIO(part["tex"])).convert("RGB")
                if im.size != (w, h):
                    im = im.resize((w, h), Image.LANCZOS)
                a = np.pad(np.asarray(im), ((gutter, gutter), (gutter, gutter), (0, 0)), mode="edge")
                img[y0:y0 + h + 2 * gutter, x0:x0 + w + 2 * gutter] = a
            uv = np.clip(part["uv"], 0.0, 1.0)            # glTF: origin top-left, samplers clamp
            u = (x0 + gutter + uv[:, 0] * w) / side
            v = 1.0 - (y0 + gutter + uv[:, 1] * h) / side
            part["uv_atlas"] = np.c_[u, v].astype(np.float32)
            part["page"] = pg
            part["tex"] = None                              # free the jpeg bytes as we go
            part.pop("uv", None)
        path = os.path.join(out_dir, "g3d_atlas_%d.jpg" % pg)
        Image.fromarray(img).save(path, quality=94, subsampling=0)
        paths.append(path)
        del img
    return paths


def plan(frame, size, api_key, max_error=6.0, focus_error=0.0, focus_radius=150.0, focus=(0.0, 0.0),
         max_tiles=20000, hole=0.0):
    """Traversal only (tileset JSON, no meshes): how many tiles a setting would download and at
    which geometric errors. Cheap - use it to size a fetch before committing to it."""
    st = {}
    urls, _ = traverse(frame, size, api_key, max_error, None, max_tiles, focus_error, focus_radius, focus, st, hole)
    t = np.array(st.get("tiles", [(0.0, 0.0)]))
    out = {"tiles": len(urls)}
    for lo, hi in ((0, 100), (100, 250), (250, 500), (500, 1e9)):
        sel = t[(t[:, 1] >= lo) & (t[:, 1] < hi)]
        out["%d-%s m" % (lo, "" if hi > 1e8 else int(hi))] = (len(sel), round(float(np.median(sel[:, 0])), 2) if len(sel) else None)
    return out


def fetch(frame, size, api_key, out_dir, max_error=3.0, progress=None, meta=None,
          focus_error=0.0, focus_radius=150.0, focus=(0.0, 0.0), max_tiles=4000, hole=0.0):
    if not api_key:
        raise net.FetchError("Photorealistic 3D Tiles need an API key (GOOGLE_MAPS_API_KEY)")
    if not ensure_dracopy():
        raise net.FetchError("DracoPy is required for Google 3D Tiles and could not be installed")
    urls, session = traverse(frame, size, api_key, max_error, progress, max_tiles,
                             focus_error, focus_radius, focus, None, hole)
    if progress:
        progress(0.2, "Google 3D Tiles: %d tiles" % len(urls))

    def job(um):
        u, m = um
        params = {"key": api_key}
        if session and "session=" not in u:
            params["session"] = session
        data = net.get(u, params=params, timeout=90).content  # held in memory only, never written
        return decode_glb(data, frame, m)

    res = net.parallel(job, urls, workers=12, progress=lambda f, m: progress and progress(0.2 + 0.6 * f, m),
                       label="3D tiles")
    parts = [p for r in res for p in r if len(p["tri"])]
    if hole > 0:
        parts = [q for q in (cut_hole(p, hole * 0.5) for p in parts) if q is not None]
    if not parts:
        raise net.FetchError("Google 3D Tiles returned no geometry for this area")
    if meta is not None:
        meta["g3d_tiles"] = len(urls)
        meta["copyright"] = "Google"
    atlas = build_atlas(parts, out_dir)
    return parts, atlas
