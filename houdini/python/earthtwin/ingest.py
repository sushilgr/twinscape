"""Ingest stage: download + project everything into a self-contained area folder.

The HDA's cook never touches the network: it only reads what this writes.

    <cache>/<area_id>/
        manifest.json        metadata, attribution, sizes
        height.exr           N x N float32 heights (m, relative to elev_ref)
        ortho.jpg            M x M orthoimage in the local frame
        buildings.bgeo.sc    footprint polygons (y = 0) + height attrib
        g3d.bgeo.sc          Google 3D tiles mesh (google mode only)
        g3d_atlas_<k>.jpg    texture atlas pages (google mode only)
    <cache>/_shared/facade_<k>.jpg   procedural facade textures
"""
import json
import os
import re
import time

import numpy as np

from . import geo, net, paths, providers

DEFAULTS = dict(
    name="area", lat=40.7075, lon=-74.0110, size=1000.0, mode="procedural",
    terrain_src="usgs_3dep", imagery_src="usgs_naip", buildings_src="auto",
    dem_res=2.0, ortho_res=0.3, max_tex=8192, g3d_error=3.0, g3d_focus_error=0.0, g3d_focus_radius=150.0, g3d_max_tiles=6000, g3d_hole=0.0,
    default_height=10.0,
    cache_dir=paths.cache_dir(),
)

PRESETS = {
    "Lower Manhattan (FiDi / One WTC)": (40.7080, -74.0115, 1200.0),
    "Midtown (Empire State)": (40.7484, -73.9857, 1000.0),
    "Times Square": (40.7580, -73.9855, 800.0),
    "Central Park South": (40.7660, -73.9760, 1200.0),
    "Hudson Yards": (40.7540, -74.0015, 900.0),
    "Brooklyn Bridge / DUMBO": (40.7040, -73.9930, 1200.0),
    "San Francisco Downtown": (37.7920, -122.3990, 1000.0),
    "Chicago Loop": (41.8810, -87.6300, 1000.0),
    "London City": (51.5145, -0.0850, 1000.0),
    "Tokyo Shinjuku": (35.6900, 139.6950, 1000.0),
}


def resolve_api_key(explicit=""):
    if explicit:
        return explicit.strip()
    k = (os.environ.get("GOOGLE_MAPS_API_KEY") or os.environ.get("TWINSCAPE_GOOGLE_API_KEY")
         or os.environ.get("EARTHTWIN_GOOGLE_API_KEY"))
    if k:
        return k.strip()
    for p in paths.key_files():
        if os.path.exists(p):
            with open(p) as f:
                return f.read().strip()
    return ""


def area_id(p):
    slug = re.sub(r"[^a-z0-9]+", "_", str(p["name"]).lower()).strip("_")[:24] or "area"
    f = lambda v: "%.4f" % float(v)
    h = net.short_hash("%.5f" % float(p["lat"]), "%.5f" % float(p["lon"]), "%.2f" % float(p["size"]), p["mode"],
                       p["terrain_src"], p["imagery_src"], p["buildings_src"], f(p["dem_res"]), f(p["ortho_res"]),
                       # 3D Tiles error only matters in google3d mode; pin it for open-data areas so
                       # changing its default never invalidates cached open-data areas
                       int(p["max_tex"]), f(p["g3d_error"] if p["mode"] == "google3d" else 3.0),
                       # street-detail LOD only joins the hash when used (keeps older cache ids)
                       *(("focus", f(p["g3d_focus_error"]), f(p["g3d_focus_radius"]))
                         if p["mode"] == "google3d" and float(p.get("g3d_focus_error") or 0) > 0 else ()),
                       *(("hole", f(p["g3d_hole"])) if p["mode"] == "google3d" and float(p.get("g3d_hole") or 0) > 0 else ()))
    return "%s_%s" % (slug, h)


def area_dir(p):
    p = dict(p, lat=round(float(p["lat"]), 5), lon=round(float(p["lon"]), 5))
    return os.path.join(p.get("cache_dir") or DEFAULTS["cache_dir"], area_id(p))


def write_exr(arr, path):
    import OpenImageIO as oiio
    arr = np.ascontiguousarray(arr.astype(np.float32).reshape(arr.shape[0], arr.shape[1], 1))
    spec = oiio.ImageSpec(arr.shape[1], arr.shape[0], 1, oiio.FLOAT)
    spec.attribute("compression", "zip")
    out = oiio.ImageOutput.create(path)
    out.open(path, spec)
    ok = out.write_image(arr)
    out.close()
    chk = np.asarray(oiio.ImageBuf(path).get_pixels(oiio.FLOAT))[..., 0]
    if not ok or not np.allclose(chk, arr[..., 0], atol=1e-3):
        raise RuntimeError("EXR write verification failed for %s" % path)


def _main_thread(fn, *a):
    """hou is not thread-safe: run geometry writes on Houdini's main thread."""
    import threading
    import hou
    # without a UI (hython, Houdini Engine / HARS) there is no event loop to defer to
    if threading.current_thread() is threading.main_thread() or not hou.isUIAvailable():
        return fn(*a)
    try:
        import hdefereval
        return hdefereval.executeInMainThreadWithResult(fn, *a)
    except ImportError:
        return fn(*a)


def _write_buildings_bgeo(blds, path):
    return _main_thread(_write_buildings_bgeo_impl, blds, path)


def _write_g3d_bgeo(parts, path):
    return _main_thread(_write_g3d_bgeo_impl, parts, path)


def _write_buildings_bgeo_impl(blds, path):
    import hou
    g = hou.Geometry()
    fattrs = ("height", "min_height", "roof_height", "roof_dir", "levels")
    iattrs = ("year", "roof_across", "is_part")
    for k in fattrs:
        g.addAttrib(hou.attribType.Prim, k, 0.0)
    for k in iattrs + ("bld_id", "height_known"):
        g.addAttrib(hou.attribType.Prim, k, 0)
    g.addAttrib(hou.attribType.Prim, "roof_shape", "")
    g.addAttrib(hou.attribType.Prim, "bcol", (0.0, 0.0, 0.0, 0.0))   # rgb + has-colour flag
    g.addAttrib(hou.attribType.Prim, "rcol", (0.0, 0.0, 0.0, 0.0))
    g.addAttrib(hou.attribType.Prim, "src_id", "")
    pos, polys = [], []
    cols = {k: [] for k in fattrs + iattrs + ("bld_id", "height_known", "roof_shape", "src_id", "bcol", "rcol")}
    for i, b in enumerate(blds):
        ring = b["rings"][0]          # outer ring (courtyard holes are not cut yet)
        start = len(pos)
        pos.extend((float(e), 0.0, float(-n)) for e, n in ring)
        polys.append(tuple(range(start, start + len(ring))))
        for k in fattrs:
            cols[k].append(float(b.get(k, 0.0)))
        for k in iattrs:
            cols[k].append(int(b.get(k, 0)))
        cols["bld_id"].append(i + 1)                      # unique, used for lookups in VEX
        cols["height_known"].append(1 if b["height"] > 0 else 0)
        cols["roof_shape"].append(str(b.get("roof_shape", "flat")))
        cols["src_id"].append(str(b["id"]))
        for k, tag in (("bcol", "colour"), ("rcol", "roof_colour")):
            c = b.get(tag)
            cols[k].extend(list(c) + [1.0] if c else [0.0, 0.0, 0.0, 0.0])
    if pos:
        g.createPoints([hou.Vector3(p) for p in pos])
        g.createPolygons(polys, True)
        for k in fattrs:
            g.setPrimFloatAttribValues(k, cols[k])
        for k in iattrs + ("bld_id", "height_known"):
            g.setPrimIntAttribValues(k, cols[k])
        g.setPrimStringAttribValues("roof_shape", cols["roof_shape"])
        g.setPrimStringAttribValues("src_id", cols["src_id"])
        g.setPrimFloatAttribValues("bcol", cols["bcol"])
        g.setPrimFloatAttribValues("rcol", cols["rcol"])
    g.saveToFile(path)
    return len(polys)


def _write_g3d_bgeo_impl(parts, path, chunk=200000):
    """Memory-lean writer: points/attributes go in as raw buffers, triangles are created in
    chunks so no multi-GB Python list of index tuples is ever built."""
    import hou
    g = hou.Geometry()
    g.addAttrib(hou.attribType.Point, "uv", (0.0, 0.0, 0.0))
    g.addAttrib(hou.attribType.Prim, "g3d_page", 0)
    g.addAttrib(hou.attribType.Prim, "g3d_tile", 0)
    npts = sum(len(p["P"]) for p in parts)
    ntri = sum(len(p["tri"]) for p in parts)
    P = np.empty((npts, 3), np.float32)
    UV = np.zeros((npts, 3), np.float32)
    T = np.empty((ntri, 3), np.int32)
    page = np.empty(ntri, np.int32)
    tile = np.empty(ntri, np.int32)
    b, o = 0, 0
    for ti, p in enumerate(parts):
        n, m = len(p["P"]), len(p["tri"])
        P[b:b + n] = p["P"]
        UV[b:b + n, :2] = p["uv_atlas"]
        T[o:o + m] = p["tri"][:, ::-1] + b          # glTF CCW -> Houdini CW winding
        page[o:o + m] = p["page"]
        tile[o:o + m] = ti
        p["P"] = p["uv_atlas"] = p["tri"] = None     # release per-tile arrays as we go
        b += n
        o += m
    g.createPoints([hou.Vector3(0, 0, 0)] * npts)
    g.setPointFloatAttribValuesFromString("P", P.tobytes())
    g.setPointFloatAttribValuesFromString("uv", UV.tobytes())
    del P, UV
    for s in range(0, ntri, chunk):
        g.createPolygons(T[s:s + chunk].tolist(), True)
    del T
    g.setPrimIntAttribValuesFromString("g3d_page", page.tobytes())
    g.setPrimIntAttribValuesFromString("g3d_tile", tile.tobytes())
    g.saveToFile(path)
    return ntri


def _inner_area_ref(p, hole):
    """A context ring must share the ground datum of the detailed area it surrounds: reuse the
    elev_ref of a cached Google 3D Tiles area at the same centre whose size equals the hole."""
    root = p["cache_dir"]
    for d in sorted(os.listdir(root)) if os.path.isdir(root) else []:
        mp = os.path.join(root, d, "manifest.json")
        if not os.path.exists(mp):
            continue
        with open(mp) as f:
            m = json.load(f)
        q = m.get("params", {})
        if (q.get("mode") == "google3d" and abs(float(q.get("size", 0)) - hole) < 1e-3
                and abs(float(q["lat"]) - p["lat"]) < 2e-5 and abs(float(q["lon"]) - p["lon"]) < 2e-5
                and not float(q.get("g3d_hole") or 0)):
            return float(m["elev_ref"])
    return None


def ensure_facades(cache_dir):
    shared = os.path.join(cache_dir, "_shared")
    paths = []
    for k in range(len(providers.FACADE_STYLES)):
        p = os.path.join(shared, "facade_%d.jpg" % k)
        pm = os.path.join(shared, "facade_%d_mask.png" % k)
        if not (os.path.exists(p) and os.path.exists(pm)):
            img, mask = providers.facade_texture(k)
            providers.save_jpg(img, p)
            from PIL import Image
            Image.fromarray(mask).save(pm)
        paths.append(p)
    return paths


def fetch(params, progress=None, force=False):
    p = dict(DEFAULTS)
    p.update({k: v for k, v in params.items() if v is not None and v != ""})
    p["size"] = float(p["size"])
    # 1e-5 deg (~1 m) quantisation: Houdini Engine passes float32 parms
    p["lat"], p["lon"] = round(float(p["lat"]), 5), round(float(p["lon"]), 5)
    key = resolve_api_key(p.pop("api_key", ""))
    out = area_dir(p)
    os.makedirs(out, exist_ok=True)
    man_path = os.path.join(out, "manifest.json")
    if os.path.exists(man_path) and not force:
        with open(man_path) as f:
            return json.load(f)

    def prog(lo, hi):
        return (lambda f, m: progress(lo + (hi - lo) * f, m)) if progress else None

    t0 = time.time()
    frame = geo.LocalFrame(p["lat"], p["lon"], 0.0)
    size = p["size"]
    meta = {}
    attribution = []
    man = dict(area_id=area_id(p), params=p, created=time.strftime("%Y-%m-%d %H:%M:%S"),
               convention="Houdini Y-up: x=East, y=Up, z=South (metres). Unreal via HE: X=East, Y=South, Z=Up.")

    man["facades"] = ensure_facades(p["cache_dir"])
    if p["mode"] == "google3d":
        # Google 3D Tiles carry their own ground + photo texture: no DEM / ortho download.
        from . import google3d
        parts, atlas = google3d.fetch(frame, size, key, out, p["g3d_error"], prog(0.02, 0.9), meta,
                                      focus_error=float(p.get("g3d_focus_error") or 0.0),
                                      focus_radius=float(p.get("g3d_focus_radius") or 150.0),
                                      max_tiles=int(p.get("g3d_max_tiles") or 6000),
                                      hole=float(p.get("g3d_hole") or 0.0))
        hole = float(p.get("g3d_hole") or 0.0)
        inner = _inner_area_ref(p, hole) if hole > 0 else None
        # ellipsoid heights -> local: ground at the centre becomes y = 0
        near = np.concatenate([q["P"][(np.abs(q["P"][:, 0]) < 60) & (np.abs(q["P"][:, 2]) < 60), 1] for q in parts])
        if len(near) <= 100:
            near = np.concatenate([q["P"][::7, 1] for q in parts])
        ref = float(np.percentile(near, 3)) if inner is None else inner
        if inner is not None:
            man["context_ring_for"] = hole
        del near
        for q in parts:
            q["P"][:, 1] -= ref
        man["g3d_triangles"] = _write_g3d_bgeo(parts, os.path.join(out, "g3d.bgeo.sc"))
        man["g3d_atlas"] = [os.path.basename(a_) for a_ in atlas]
        man["g3d_tiles"] = meta.get("g3d_tiles", 0)
        man["google_content"] = True
        n = 17
        write_exr(np.zeros((n, n), np.float32), os.path.join(out, "height.exr"))
        man.update(dem_n=n, elev_ref=ref, elev_min=0.0, elev_max=0.0, ortho_px=256, ortho_res=size / 256.0,
                   buildings=0, height_datum="WGS84 ellipsoid (Google 3D Tiles)")
        providers.save_jpg(np.full((256, 256, 3), 128, np.uint8), os.path.join(out, "ortho.jpg"))
        attribution.append("3D: Google Photorealistic 3D Tiles (Map data (c) Google)")
    else:
        # ---- terrain
        n = int(np.clip(round(size / p["dem_res"]) + 1, 17, 2049))
        if progress:
            progress(0.01, "Terrain (%s, %d x %d)" % (p["terrain_src"], n, n))
        tsrc = p["terrain_src"]
        try:
            h = providers.terrain(tsrc, frame, size, n, p["cache_dir"], key, prog(0.0, 0.25))
        except net.FetchError as ex:
            if tsrc == "aws_terrarium":
                raise
            # USGS / Google outage: fall back to the global open DEM rather than failing the build
            if progress:
                progress(0.05, "Terrain source failed (%s) - falling back to AWS Terrarium" % str(ex)[:60])
            tsrc = "aws_terrarium"
            h = providers.terrain(tsrc, frame, size, n, p["cache_dir"], key, prog(0.0, 0.25))
            man["terrain_fallback"] = str(ex)[:200]
        p_terrain_used = tsrc
        elev_ref = float(h[n // 2, n // 2])
        write_exr(h - elev_ref, os.path.join(out, "height.exr"))
        man.update(dem_n=n, elev_ref=elev_ref, elev_min=float(h.min()), elev_max=float(h.max()))
        man["terrain_src_used"] = p_terrain_used
        if p_terrain_used in providers.ATTRIBUTION:
            attribution.append(providers.ATTRIBUTION[p_terrain_used])
        # ---- imagery (power-of-two so Unreal builds a full mip chain / VT pages)
        want = max(256.0, size / p["ortho_res"])
        m = int(min(p["max_tex"], 2 ** int(round(np.log2(want)))))
        if progress:
            progress(0.25, "Imagery (%s, %d px)" % (p["imagery_src"], m))
        img = providers.imagery(p["imagery_src"], frame, size, m, p["cache_dir"], key, prog(0.25, 0.6), meta)
        providers.save_jpg(img, os.path.join(out, "ortho.jpg"))
        from PIL import Image as _Im
        providers.save_jpg(np.asarray(_Im.fromarray(img).resize((512, 512))), os.path.join(out, "ortho_preview.jpg"), q=85)
        man.update(ortho_px=m, ortho_res=size / m)
        if p["imagery_src"] in providers.ATTRIBUTION:
            attribution.append(providers.ATTRIBUTION[p["imagery_src"]])
        if p["imagery_src"] == "google_satellite":
            man["google_content"] = True
        # ---- buildings
        if progress:
            progress(0.6, "Buildings (%s)" % p["buildings_src"])
        blds, used = providers.buildings(p["buildings_src"], frame, size, p["cache_dir"], prog(0.6, 0.9),
                                         p["default_height"])
        man["buildings"] = _write_buildings_bgeo(blds, os.path.join(out, "buildings.bgeo.sc")) if blds else 0
        man["buildings_src_used"] = used
        man["building_parts"] = sum(1 for b in blds if b.get("is_part"))
        man["pitched_roofs"] = sum(1 for b in blds if b.get("roof_shape", "flat") != "flat")
        if used in providers.ATTRIBUTION:
            attribution.append(providers.ATTRIBUTION[used])
    if "copyright" in meta:
        attribution.append(meta["copyright"])
    man["attribution"] = attribution
    man["seconds"] = round(time.time() - t0, 1)
    with open(man_path, "w") as f:
        json.dump(man, f, indent=1)
    if progress:
        progress(1.0, "Done in %.0fs" % man["seconds"])
    return man


def purge_google(cache_dir=None):
    """Delete every cached area that holds Google-derived content (3D Tiles mesh/atlas or satellite)."""
    import shutil
    cache_dir = cache_dir or DEFAULTS["cache_dir"]
    gone = []
    for man in list_areas(cache_dir):
        if man.get("google_content"):
            d = os.path.join(cache_dir, man["area_id"])
            shutil.rmtree(d, ignore_errors=True)
            gone.append(man["area_id"])
    return gone


def list_areas(cache_dir=None):
    cache_dir = cache_dir or DEFAULTS["cache_dir"]
    out = []
    if not os.path.isdir(cache_dir):
        return out
    for d in sorted(os.listdir(cache_dir)):
        mp = os.path.join(cache_dir, d, "manifest.json")
        if os.path.exists(mp):
            with open(mp) as f:
                out.append(json.load(f))
    return out
