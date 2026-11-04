"""Data providers: terrain, orthoimagery and building footprints.

Every provider works on the same target: a square of side `size` metres in the
local ENU frame, centred on the frame origin.

Terrain   -> float32 array (N, N), row 0 = north edge, col 0 = west edge,
             sampled exactly at Houdini Grid SOP point positions.
Imagery   -> uint8 array (M, M, 3), same orientation, pixel-centre aligned.
Buildings -> list of dicts {id, height, rings:[np.array (k,2) east/north]}.
"""
import io
import math
import os

import numpy as np
from PIL import Image

from . import geo, net

Image.MAX_IMAGE_PIXELS = None

# --------------------------------------------------------------------------
# Catalogue (shown in the UI / HDA menus). licence text is informational.
TERRAIN_SOURCES = [
    ("usgs_3dep", "USGS 3DEP (US, ~1 m lidar DEM, public domain)"),
    ("aws_terrarium", "AWS Terrain Tiles / Terrarium (global, public)"),
    ("google_elevation", "Google Elevation API (global, API key)"),
    ("flat", "Flat (no terrain)"),
]
IMAGERY_SOURCES = [
    ("usgs_naip", "USGS NAIP (US, 0.3-0.6 m, public domain)"),
    ("esri_world", "Esri World Imagery (global, attribution, check licence)"),
    ("google_satellite", "Google Map Tiles 2D satellite (API key)"),
    ("none", "None (grey)"),
]
BUILDING_SOURCES = [
    ("auto", "Auto: OSM 3D (parts + roof shapes), NYC heights filled in NYC"),
    ("osm3d", "OpenStreetMap 3D: building parts + roof shapes (ODbL)"),
    ("osm", "OpenStreetMap outlines only (ODbL)"),
    ("nyc_open_data", "NYC Open Data footprints + roof heights (flat roofs)"),
    ("none", "None"),
]
ATTRIBUTION = {
    "usgs_3dep": "Elevation: USGS 3D Elevation Program",
    "aws_terrarium": "Elevation: Mapzen/AWS Terrain Tiles (USGS, SRTM, others)",
    "google_elevation": "Elevation: Google",
    "usgs_naip": "Imagery: USDA NAIP via USGS The National Map",
    "esri_world": "Imagery: Esri, Maxar, Earthstar Geographics, and the GIS User Community",
    "google_satellite": "Imagery: Google (see manifest copyright)",
    "nyc_open_data": "Buildings: NYC Open Data, Building Footprints",
    "osm": "Buildings: (c) OpenStreetMap contributors, ODbL",
    "osm3d": "Buildings: (c) OpenStreetMap contributors, ODbL",
    "osm3d+nyc_heights": "Buildings: (c) OpenStreetMap contributors (ODbL); heights NYC Open Data",
    "osm+nyc_heights": "Buildings: (c) OpenStreetMap contributors (ODbL); heights NYC Open Data",
}


def _progress(cb, f, msg):
    if cb:
        cb(f, msg)


def grid_points(frame, size, n):
    """ENU coords of an n x n grid matching Houdini's Grid SOP layout
    (rows north->south, columns west->east)."""
    t = np.linspace(-size * 0.5, size * 0.5, n)
    e, nn = np.meshgrid(t, t[::-1])
    return e, nn


def pixel_centres(frame, size, m):
    t = -size * 0.5 + (np.arange(m) + 0.5) * size / m
    e, n = np.meshgrid(t, t[::-1])
    return e, n


# ============================================================== TERRAIN
def terrain(src, frame, size, n, cache_dir, api_key=None, progress=None):
    if src == "flat":
        return np.zeros((n, n), np.float32)
    e, nn = grid_points(frame, size, n)
    lat, lon, _ = frame.enu_to_ll(e, nn)
    if src == "usgs_3dep":
        h = _terrain_3dep(frame, size, lat, lon, cache_dir, progress)
    elif src == "aws_terrarium":
        h = _terrain_terrarium(frame, size, n, lat, lon, cache_dir, progress)
    elif src == "google_elevation":
        h = _terrain_google(lat, lon, api_key, progress)
    else:
        raise ValueError("unknown terrain source %s" % src)
    h = np.asarray(h, np.float32)
    bad = ~np.isfinite(h) | (h < -500) | (h > 9000)
    if bad.any():
        h[bad] = np.nanmedian(np.where(bad, np.nan, h)) if (~bad).any() else 0.0
    return h


def _read_float_tiff(data):
    try:
        import OpenImageIO as oiio
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".tif", delete=False) as t:
            t.write(data)
            p = t.name
        buf = oiio.ImageBuf(p)
        arr = buf.get_pixels(oiio.FLOAT)
        os.unlink(p)
        return np.asarray(arr)[..., 0]
    except Exception:
        return np.asarray(Image.open(io.BytesIO(data)), np.float32)


def _terrain_3dep(frame, size, lat, lon, cache_dir, progress):
    s, w, n_, e = frame.bbox_ll(size, margin=size * 0.02 + 20)
    # ~1 m sampling, capped per request; tiled requests for large areas
    deg_m = 111320.0
    span_x = (e - w) * deg_m * math.cos(math.radians(frame.lat))
    span_y = (n_ - s) * deg_m
    res = max(1.0, max(span_x, span_y) / 6000.0)
    W, H = int(math.ceil(span_x / res)), int(math.ceil(span_y / res))
    chunk = 2000
    out = np.zeros((H, W), np.float32)
    jobs = []
    for r0 in range(0, H, chunk):
        for c0 in range(0, W, chunk):
            jobs.append((r0, c0, min(chunk, H - r0), min(chunk, W - c0)))

    def job(j):
        r0, c0, hh, ww = j
        bw = w + (e - w) * c0 / W
        be = w + (e - w) * (c0 + ww) / W
        bn = n_ - (n_ - s) * r0 / H
        bs = n_ - (n_ - s) * (r0 + hh) / H
        url = "https://elevation.nationalmap.gov/arcgis/rest/services/3DEPElevation/ImageServer/exportImage"
        params = dict(bbox="%.8f,%.8f,%.8f,%.8f" % (bw, bs, be, bn), bboxSR=4326, imageSR=4326,
                      size="%d,%d" % (ww, hh), format="tiff", pixelType="F32",
                      interpolation="RSP_BilinearInterpolation", noData=-9999, f="image")
        cp = os.path.join(cache_dir, "dem3dep", net.short_hash(params["bbox"], params["size"]) + ".tif")
        data = net.cached_get(url, cp, params=params)
        a = _read_float_tiff(data)
        out[r0:r0 + hh, c0:c0 + ww] = a[:hh, :ww]

    net.parallel(job, jobs, workers=4, progress=progress, label="3DEP DEM")
    out[out < -1000] = np.nan
    if np.isnan(out).all():
        raise net.FetchError("3DEP returned no data here (outside US coverage?)")
    out = np.where(np.isnan(out), 0.0, out)  # water / no data -> sea level
    px = (lon - w) / (e - w) * W
    py = (n_ - lat) / (n_ - s) * H
    return geo.bilinear(out, px, py)


def _terrain_terrarium(frame, size, n, lat, lon, cache_dir, progress):
    res = size / max(1, n - 1)
    z = min(15, geo.zoom_for_res(frame.lat, res))
    s, w, n_, e = frame.bbox_ll(size, margin=size * 0.02 + 50)
    x0, y0 = geo.ll_to_tile(n_, w, z)
    x1, y1 = geo.ll_to_tile(s, e, z)
    tx = range(int(x0), int(x1) + 1)
    ty = range(int(y0), int(y1) + 1)
    mosaic = np.zeros((len(ty) * 256, len(tx) * 256), np.float32)
    jobs = [(i, j, x, y) for j, y in enumerate(ty) for i, x in enumerate(tx)]

    def job(jb):
        i, j, x, y = jb
        url = "https://s3.amazonaws.com/elevation-tiles-prod/terrarium/%d/%d/%d.png" % (z, x, y)
        data = net.cached_get(url, os.path.join(cache_dir, "terrarium", str(z), str(x), "%d.png" % y))
        a = np.asarray(Image.open(io.BytesIO(data)).convert("RGB"), np.float32)
        mosaic[j * 256:(j + 1) * 256, i * 256:(i + 1) * 256] = (a[..., 0] * 256 + a[..., 1] + a[..., 2] / 256.0) - 32768.0

    net.parallel(job, jobs, workers=16, progress=progress, label="Terrarium")
    mx, my = geo.ll_to_merc(lat, lon)
    gx, gy = geo.merc_to_global_px(mx, my, z)
    return geo.bilinear(mosaic, gx - tx[0] * 256, gy - ty[0] * 256)


def _terrain_google(lat, lon, api_key, progress):
    if not api_key:
        raise net.FetchError("Google Elevation needs an API key (GOOGLE_MAPS_API_KEY)")
    pts = list(zip(lat.ravel().tolist(), lon.ravel().tolist()))
    batch = 256
    chunks = [pts[i:i + batch] for i in range(0, len(pts), batch)]

    def job(c):
        r = net.get("https://maps.googleapis.com/maps/api/elevation/json",
                    params={"locations": "enc:" + geo.encode_polyline(c), "key": api_key})
        js = r.json()
        if js.get("status") != "OK":
            raise net.FetchError("Elevation API: %s %s" % (js.get("status"), js.get("error_message", "")))
        return [x["elevation"] for x in js["results"]]

    res = net.parallel(job, chunks, workers=8, progress=progress, label="Google Elevation")
    flat = [v for c in res for v in c]
    return np.array(flat, np.float32).reshape(lat.shape)


# ============================================================== IMAGERY
XYZ = {
    "esri_world": ("https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}", 19),
}


def imagery(src, frame, size, m, cache_dir, api_key=None, progress=None, meta=None):
    meta = meta if meta is not None else {}
    if src == "none":
        return np.full((m, m, 3), 128, np.uint8)
    target_res = size / float(m)
    if src == "usgs_naip":
        mos, mx0, my1, mres = _naip_mosaic(frame, size, target_res, cache_dir, progress)
    elif src in XYZ:
        tmpl, zmax = XYZ[src]
        z = min(zmax, geo.zoom_for_res(frame.lat, target_res))
        mos, mx0, my1, mres = _xyz_mosaic(tmpl, z, frame, size, cache_dir, src, progress)
    elif src == "google_satellite":
        mos, mx0, my1, mres = _google_2d_mosaic(frame, size, target_res, api_key, progress, meta)
    else:
        raise ValueError("unknown imagery source %s" % src)
    _progress(progress, 0.95, "Resampling imagery to local frame")
    out = np.zeros((m, m, 3), np.uint8)
    e, n = pixel_centres(frame, size, m)
    step = 512
    for r0 in range(0, m, step):
        lat, lon, _ = frame.enu_to_ll(e[r0:r0 + step], n[r0:r0 + step])
        mx, my = geo.ll_to_merc(lat, lon)
        px = (mx - mx0) / mres
        py = (my1 - my) / mres
        out[r0:r0 + step] = np.clip(geo.bilinear(mos, px, py), 0, 255).astype(np.uint8)
    return out


def _merc_bbox(frame, size, margin):
    s, w, n, e = frame.bbox_ll(size, margin=margin)
    x0, y0 = geo.ll_to_merc(s, w)
    x1, y1 = geo.ll_to_merc(n, e)
    return float(x0), float(y0), float(x1), float(y1)


def _naip_mosaic(frame, size, target_res, cache_dir, progress):
    x0, y0, x1, y1 = _merc_bbox(frame, size, size * 0.01 + 10)
    k = 1.0 / math.cos(math.radians(frame.lat))  # mercator stretch
    mres = max(0.3, target_res) * k
    W, H = int(math.ceil((x1 - x0) / mres)), int(math.ceil((y1 - y0) / mres))
    chunk = 2048
    mos = np.zeros((H, W, 3), np.uint8)
    jobs = [(r, c, min(chunk, H - r), min(chunk, W - c)) for r in range(0, H, chunk) for c in range(0, W, chunk)]

    def job(j):
        r0, c0, hh, ww = j
        bx0 = x0 + c0 * mres
        by1 = y1 - r0 * mres
        bbox = "%.3f,%.3f,%.3f,%.3f" % (bx0, by1 - hh * mres, bx0 + ww * mres, by1)
        params = dict(bbox=bbox, bboxSR=3857, imageSR=3857, size="%d,%d" % (ww, hh),
                      format="jpg", compressionQuality=92, interpolation="RSP_BilinearInterpolation", f="image")
        url = "https://imagery.nationalmap.gov/arcgis/rest/services/USGSNAIPPlus/ImageServer/exportImage"
        data = net.cached_get(url, os.path.join(cache_dir, "naip", net.short_hash(bbox, ww, hh) + ".jpg"), params=params)
        a = np.asarray(Image.open(io.BytesIO(data)).convert("RGB"))
        mos[r0:r0 + hh, c0:c0 + ww] = a[:hh, :ww]

    net.parallel(job, jobs, workers=6, progress=progress, label="NAIP")
    return mos.astype(np.float32), x0, y1, mres


def _xyz_mosaic(tmpl, z, frame, size, cache_dir, name, progress, params=None, ext="jpg",
                tile_size=256, persist=True):
    s, w, n, e = frame.bbox_ll(size, margin=size * 0.01 + 10)
    fx0, fy0 = geo.ll_to_tile(n, w, z)
    fx1, fy1 = geo.ll_to_tile(s, e, z)
    tx = list(range(int(fx0), int(fx1) + 1))
    ty = list(range(int(fy0), int(fy1) + 1))
    mos = np.zeros((len(ty) * tile_size, len(tx) * tile_size, 3), np.uint8)
    jobs = [(i, j, x, y) for j, y in enumerate(ty) for i, x in enumerate(tx)]

    def job(jb):
        i, j, x, y = jb
        url = tmpl.format(z=z, x=x, y=y)
        cp = os.path.join(cache_dir, name, str(z), str(x), "%d.%s" % (y, ext)) if persist else None
        data = net.cached_get(url, cp, params=params, ok_404=True)
        if data:
            a = np.asarray(Image.open(io.BytesIO(data)).convert("RGB").resize((tile_size, tile_size)))
            mos[j * tile_size:(j + 1) * tile_size, i * tile_size:(i + 1) * tile_size] = a

    net.parallel(job, jobs, workers=16, progress=progress, label=name)
    mres = 2 * geo.MERC_MAX / (tile_size * 2 ** z)
    mx0 = tx[0] * tile_size * mres - geo.MERC_MAX
    my1 = geo.MERC_MAX - ty[0] * tile_size * mres
    return mos.astype(np.float32), mx0, my1, mres


def _google_2d_mosaic(frame, size, target_res, api_key, progress, meta):
    if not api_key:
        raise net.FetchError("Google Map Tiles needs an API key (GOOGLE_MAPS_API_KEY)")
    r = net.get("https://tile.googleapis.com/v1/createSession", method="POST", params={"key": api_key},
                json={"mapType": "satellite", "language": "en-US", "region": "US"})
    sess = r.json()
    ts = int(sess.get("tileWidth", 256))
    z = min(22, geo.zoom_for_res(frame.lat, target_res, tile_size=ts))
    params = {"session": sess["session"], "key": api_key}
    tmpl = "https://tile.googleapis.com/v1/2dtiles/{z}/{x}/{y}"
    # Google content is not persisted as raw tiles (Map Tiles API caching policy).
    out = _xyz_mosaic(tmpl, z, frame, size, None, "google2d", progress, params=params,
                      tile_size=ts, persist=False)
    try:
        s, w, n, e = frame.bbox_ll(size)
        vi = net.get("https://tile.googleapis.com/tile/v1/viewport",
                     params={"session": sess["session"], "key": api_key, "zoom": z,
                             "north": n, "south": s, "east": e, "west": w}).json()
        meta["copyright"] = vi.get("copyright", "Google")
    except Exception:
        meta["copyright"] = "Google"
    return out


# ============================================================== BUILDINGS
NYC_BOUNDS = (40.49, -74.27, 40.92, -73.68)  # s, w, n, e
ROOF_SHAPES = ["flat", "skillion", "gabled", "hipped", "pyramidal", "dome", "onion", "mansard",
               "gambrel", "round", "half-hipped", "saltbox", "cone"]


def in_nyc(lat, lon):
    s, w, n, e = NYC_BOUNDS
    return s <= lat <= n and w <= lon <= e


def _blank(bid, rings, height=0.0, **kw):
    b = dict(id=bid, rings=rings, height=height, min_height=0.0, roof_shape="flat", roof_height=-1.0,
             roof_dir=-1.0, roof_across=0, is_part=0, year=0, levels=0.0)
    b.update(kw)
    return b


def buildings(src, frame, size, cache_dir, progress=None, default_h=10.0):
    """-> (list of buildings in local ENU, source used). Each building:
    rings (ENU (k,2) arrays, outer first), height (m above ground, 0 = unknown), min_height,
    roof_shape, roof_height (-1 = unknown), roof_dir (compass deg of downslope, -1 = unknown),
    roof_across, is_part."""
    if src == "none":
        return [], "none"
    s, w, n, e = frame.bbox_ll(size, margin=5)
    if src == "auto":
        src = "osm3d"
    used = src
    if src == "nyc_open_data":
        raw = _nyc_buildings(s, w, n, e, progress)
    elif src in ("osm", "osm3d"):
        try:
            raw = _osm_buildings(s, w, n, e, progress, default_h, cache_dir, parts=(src == "osm3d"))
        except net.FetchError:
            if not in_nyc(frame.lat, frame.lon):
                raise
            raw, used = _nyc_buildings(s, w, n, e, progress), "nyc_open_data"
        if used != "nyc_open_data" and in_nyc(frame.lat, frame.lon):
            try:
                _fill_heights_from_nyc(raw, _nyc_buildings(s, w, n, e, progress))
                used = src + "+nyc_heights"
            except Exception:
                pass
    else:
        raise ValueError("unknown building source %s" % src)
    half = size * 0.5
    out = []
    for b in raw:
        rings = []
        for ring in b["rings"]:
            ring = np.asarray(ring, np.float64)
            if len(ring) < 3:
                continue
            enu = frame.ll_to_enu(ring[:, 1], ring[:, 0], 0.0)
            pts = enu[:, :2]
            if len(pts) > 1 and np.allclose(pts[0], pts[-1]):
                pts = pts[:-1]
            if len(pts) >= 3:
                rings.append(pts)
        if not rings:
            continue
        c = rings[0].mean(axis=0)
        if abs(c[0]) > half or abs(c[1]) > half:
            continue
        b["rings"] = rings
        out.append(b)
    return out, used


def _nyc_buildings(s, w, n, e, progress):
    url = "https://data.cityofnewyork.us/resource/5zhs-2jue.json"
    out, off, page = [], 0, 20000
    while True:
        poly = "POLYGON((%f %f,%f %f,%f %f,%f %f,%f %f))" % (w, s, e, s, e, n, w, n, w, s)
        params = {"$where": "intersects(the_geom,'%s')" % poly,
                  "$select": "bin,doitt_id,height_roof,ground_elevation,construction_year,the_geom",
                  "$limit": page, "$offset": off, "$order": "doitt_id"}
        rows = net.get(url, params=params, timeout=120).json()
        for r in rows:
            g = r.get("the_geom") or {}
            try:
                h = float(r.get("height_roof") or 0) * 0.3048
            except ValueError:
                h = 0.0
            polys = g.get("coordinates", []) if g.get("type") == "MultiPolygon" else [g.get("coordinates", [])]
            for k, poly in enumerate(polys):
                if not poly:
                    continue
                out.append(_blank(int(r.get("doitt_id") or r.get("bin") or 0) * 10 + k, [poly[0]],
                                  h if h > 1.0 else 0.0, year=int(float(r.get("construction_year") or 0))))
        _progress(progress, 0.5, "NYC buildings %d" % len(out))
        if len(rows) < page:
            break
        off += page
    return out


OVERPASS = ["https://overpass-api.de/api/interpreter",
            "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
            "https://overpass.private.coffee/api/interpreter",
            "https://overpass.kumi.systems/api/interpreter"]


def _num(v):
    try:
        return float(str(v).lower().replace("m", "").replace(",", ".").split(";")[0].strip())
    except (ValueError, TypeError):
        return None


_COMPASS = {"N": 0, "NNE": 22.5, "NE": 45, "ENE": 67.5, "E": 90, "ESE": 112.5, "SE": 135, "SSE": 157.5,
            "S": 180, "SSW": 202.5, "SW": 225, "WSW": 247.5, "W": 270, "WNW": 292.5, "NW": 315, "NNW": 337.5}


_CSS = {"white": "#f2f2ee", "black": "#2a2a2a", "grey": "#8c8c8c", "gray": "#8c8c8c", "silver": "#c0c0c0",
        "red": "#9a3b2e", "brown": "#7a5a43", "maroon": "#6e2a22", "beige": "#d8cbb0", "tan": "#c8a97e",
        "yellow": "#d8c27a", "orange": "#c77d3a", "blue": "#5a7894", "lightblue": "#9db8cc", "darkblue": "#2f4660",
        "green": "#5f7a55", "darkgreen": "#3d5236", "lightgrey": "#c8c8c4", "lightgray": "#c8c8c4",
        "darkgrey": "#555555", "darkgray": "#555555", "cream": "#efe6cf", "sandstone": "#c9b38a", "brick": "#8e4a36",
        "pink": "#d9a7a0", "purple": "#6c5677", "teal": "#3f7f7f", "navy": "#24324a", "olive": "#6b6b3a",
        "gold": "#c9a54a", "ivory": "#f1ead8", "khaki": "#bfb07a"}


def _colour(v):
    """OSM colour tag -> linear-ish 0..1 rgb tuple, or None."""
    if not v:
        return None
    v = str(v).strip().lower().replace(" ", "").split(";")[0]
    v = _CSS.get(v, v)
    if v.startswith("#") and len(v) in (4, 7):
        h = v[1:]
        if len(h) == 3:
            h = "".join(ch * 2 for ch in h)
        try:
            return tuple(int(h[i:i + 2], 16) / 255.0 for i in (0, 2, 4))
        except ValueError:
            return None
    return None


def _tags_to_building(bid, rings, t, is_part):
    lv = _num(t.get("building:levels"))
    h = _num(t.get("height"))
    if h is None and lv:
        h = lv * 3.2 + (_num(t.get("roof:levels")) or 0) * 2.8 + 1.0
    mh = _num(t.get("min_height"))
    if mh is None and _num(t.get("building:min_level")):
        mh = _num(t.get("building:min_level")) * 3.2
    rs = str(t.get("roof:shape", "flat")).lower().strip()
    rs = {"pyramid": "pyramidal", "pitched": "gabled", "gable": "gabled", "hip": "hipped",
          "half_hipped": "half-hipped", "lean_to": "skillion", "shed": "skillion", "domed": "dome",
          "round": "round", "conical": "cone"}.get(rs, rs)
    if rs not in ROOF_SHAPES:
        rs = "flat"
    rh = _num(t.get("roof:height"))
    if rh is None and _num(t.get("roof:levels")):
        rh = _num(t.get("roof:levels")) * 2.8
    rd = t.get("roof:direction")
    rdir = _COMPASS.get(str(rd).upper(), _num(rd)) if rd is not None else None
    return _blank(bid, rings, h or 0.0, min_height=mh or 0.0, roof_shape=rs,
                  roof_height=rh if rh is not None else -1.0,
                  roof_dir=float(rdir) % 360.0 if rdir is not None else -1.0,
                  roof_across=1 if str(t.get("roof:orientation", "")).lower() == "across" else 0,
                  is_part=1 if is_part else 0, levels=lv or 0.0,
                  colour=_colour(t.get("building:colour") or t.get("colour")),
                  roof_colour=_colour(t.get("roof:colour")))


def _overpass(q, cache_dir, progress):
    import json as _json
    cp = os.path.join(cache_dir, "osm", net.short_hash(q) + ".json") if cache_dir else None
    if cp and os.path.exists(cp):
        with open(cp) as f:
            return _json.load(f)
    js, err = None, None
    for i in range(8):
        url = OVERPASS[i % len(OVERPASS)]
        try:
            js = net.get(url, method="POST", data={"data": q}, retries=1, timeout=120).json()
            break
        except Exception as ex:
            err = ex
            _progress(progress, 0.1, "Overpass busy, trying %s" % url.split("/")[2])
    if js is None:
        raise net.FetchError("Overpass unavailable: %s" % err)
    if cp:
        os.makedirs(os.path.dirname(cp), exist_ok=True)
        with open(cp, "w") as f:
            _json.dump(js, f)
    return js


def _pip(px, py, poly):
    """vectorised point-in-polygon (even-odd); poly (k,2)."""
    x, y = poly[:, 0], poly[:, 1]
    x2, y2 = np.roll(x, -1), np.roll(y, -1)
    inside = np.zeros(np.shape(px), bool)
    for i in range(len(poly)):
        c = ((y[i] > py) != (y2[i] > py)) & (px < (x2[i] - x[i]) * (py - y[i]) / (y2[i] - y[i] + 1e-30) + x[i])
        inside ^= c
    return inside


def _osm_buildings(s, w, n, e, progress, default_h, cache_dir=None, parts=True):
    bb = "(%f,%f,%f,%f)" % (s, w, n, e)
    sel = 'way["building"]%s;relation["building"]["type"="multipolygon"]%s;' % (bb, bb)
    if parts:
        sel += 'way["building:part"]%s;relation["building:part"]["type"="multipolygon"]%s;' % (bb, bb)
    q = "[out:json][timeout:180];(%s);out body;>;out skel qt;" % sel
    js = _overpass(q, cache_dir, progress)
    nodes = {el["id"]: (el["lon"], el["lat"]) for el in js["elements"] if el["type"] == "node"}
    ways = {el["id"]: el for el in js["elements"] if el["type"] == "way"}
    outlines, partl, used = [], [], set()

    def ring_of(way):
        return [nodes[i] for i in way.get("nodes", []) if i in nodes]

    for el in js["elements"]:
        t = el.get("tags", {})
        if el["type"] != "relation" or not ("building" in t or "building:part" in t):
            continue
        is_part = "building:part" in t and "building" not in t
        outers = [m for m in el.get("members", []) if m["type"] == "way" and m.get("role") == "outer"
                  and m["ref"] in ways]
        inners = [ring_of(ways[m["ref"]]) for m in el.get("members", []) if m["type"] == "way"
                  and m.get("role") == "inner" and m["ref"] in ways]
        for k, m in enumerate(outers):
            used.add(m["ref"])
            b = _tags_to_building(el["id"] * 100 + k, [ring_of(ways[m["ref"]])] + inners, t, is_part)
            (partl if is_part else outlines).append(b)
    for wid, wy in ways.items():
        t = wy.get("tags", {})
        if wid in used or not ("building" in t or "building:part" in t):
            continue
        is_part = "building:part" in t and "building" not in t
        b = _tags_to_building(wid, [ring_of(wy)], t, is_part)
        (partl if is_part else outlines).append(b)
    # OSM simple-3D convention: an outline that contains parts is replaced by its parts
    if partl:
        pc = np.array([np.mean(np.asarray(p["rings"][0], np.float64), axis=0) for p in partl])
        keep = []
        for o in outlines:
            ring = np.asarray(o["rings"][0], np.float64)
            lo, hi = ring.min(axis=0), ring.max(axis=0)
            cand = np.nonzero((pc[:, 0] >= lo[0]) & (pc[:, 0] <= hi[0]) & (pc[:, 1] >= lo[1]) & (pc[:, 1] <= hi[1]))[0]
            if len(cand) and _pip(pc[cand, 0], pc[cand, 1], ring).any():
                for i in cand[_pip(pc[cand, 0], pc[cand, 1], ring)]:
                    p = partl[i]
                    if p["height"] <= 0 and o["height"] > 0:   # inherit from outline
                        p["height"] = o["height"]
                continue
            keep.append(o)
        outlines = keep
    _progress(progress, 0.6, "OSM: %d buildings + %d parts" % (len(outlines), len(partl)))
    return outlines + partl


def _fill_heights_from_nyc(osm, nyc):
    """Fill missing OSM heights from NYC footprint roof heights (centroid-in-polygon)."""
    if not nyc:
        return
    rings = [np.asarray(b["rings"][0], np.float64) for b in nyc]
    los = np.array([r.min(axis=0) for r in rings])
    his = np.array([r.max(axis=0) for r in rings])
    for b in osm:
        if b["height"] > 0 or b["is_part"]:
            continue
        c = np.mean(np.asarray(b["rings"][0], np.float64), axis=0)
        cand = np.nonzero((los[:, 0] <= c[0]) & (his[:, 0] >= c[0]) & (los[:, 1] <= c[1]) & (his[:, 1] >= c[1]))[0]
        for i in cand:
            if _pip(np.array([c[0]]), np.array([c[1]]), rings[i])[0]:
                b["height"] = nyc[i]["height"]
                break


# ============================================================== FACADES
FACADE_STYLES = [
    # wall rgb, glass rgb, window w frac, window h frac, mullions, grime
    ((196, 186, 170), (52, 66, 80), 0.55, 0.60, 2, 0.25),   # limestone, punched windows
    ((150, 82, 62), (44, 52, 60), 0.45, 0.58, 1, 0.30),      # brick, NYC walk-up
    ((92, 104, 112), (70, 98, 122), 0.90, 0.86, 3, 0.10),    # glass curtain wall
    ((214, 212, 206), (60, 74, 86), 0.70, 0.55, 2, 0.18),    # modern white grid
]


def facade_texture(style, px=1024, bays=4, floors=4, seed=0):
    rng = np.random.default_rng(seed + 17 * style)
    wall, glass, wf, hf, mull, grime = FACADE_STYLES[style]
    img = np.ones((px, px, 3), np.float32) * np.array(wall, np.float32)
    mask = np.zeros((px, px), np.float32)
    # low-frequency grime / colour variation
    noise = Image.fromarray((rng.random((64, 64)) * 255).astype(np.uint8)).resize((px, px), Image.BICUBIC)
    nz = (np.asarray(noise, np.float32) / 255.0 - 0.5)
    img *= (1.0 + nz[..., None] * grime * 0.6)
    bw, fh = px / bays, px / floors
    yy, xx = np.mgrid[0:px, 0:px].astype(np.float32)
    for f in range(floors):
        for b in range(bays):
            cx, cy = (b + 0.5) * bw, (f + 0.5) * fh + fh * 0.05
            hw, hh = bw * wf * 0.5, fh * hf * 0.5
            m = (np.abs(xx - cx) < hw) & (np.abs(yy - cy) < hh)
            tone = 0.75 + 0.5 * rng.random()          # per-window reflection / blinds
            grad = 1.0 + 0.35 * ((yy - (cy - hh)) / (2 * hh) - 0.5)  # sky reflection gradient
            g = np.array(glass, np.float32) * tone
            img[m] = (g[None, :] * grad[m][:, None])
            mask[m] = 1.0
            # frames / mullions
            frame = m & ((np.abs(np.abs(xx - cx) - hw) < 3) | (np.abs(np.abs(yy - cy) - hh) < 3))
            for k in range(1, mull + 1):
                frame |= m & (np.abs(xx - (cx - hw + 2 * hw * k / (mull + 1))) < 2)
            img[frame] = np.array(wall, np.float32) * 0.55
            mask[frame] = 0.0
            # sill
            sill = (np.abs(xx - cx) < hw + 4) & (np.abs(yy - (cy + hh + 4)) < 3)
            img[sill] *= 0.7
    # floor slab band
    band = (yy % fh) < fh * 0.06
    img[band] *= 0.82
    return np.clip(img, 0, 255).astype(np.uint8), (mask * 255).astype(np.uint8)


def save_jpg(arr, path, q=92):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    Image.fromarray(arr).save(path, quality=q, subsampling=0)
