"""Geodesy helpers: WGS84 <-> ECEF <-> local ENU, Web Mercator tile math.

Local frame convention (Houdini, Y-up, metres):
    P.x = East, P.y = Up, P.z = -North   (so +Z is South)
Houdini Engine maps this to Unreal as X=East, Y=South, Z=Up (cm), which is the
same convention Cesium for Unreal's georeference uses.
"""
import math
import numpy as np

A = 6378137.0
F = 1.0 / 298.257223563
E2 = F * (2.0 - F)
B = A * (1.0 - F)
EP2 = (A * A - B * B) / (B * B)
MERC_R = 6378137.0
MERC_MAX = math.pi * MERC_R


def lla_to_ecef(lat, lon, h=0.0):
    lat = np.radians(np.asarray(lat, dtype=np.float64))
    lon = np.radians(np.asarray(lon, dtype=np.float64))
    h = np.asarray(h, dtype=np.float64)
    s = np.sin(lat)
    n = A / np.sqrt(1.0 - E2 * s * s)
    x = (n + h) * np.cos(lat) * np.cos(lon)
    y = (n + h) * np.cos(lat) * np.sin(lon)
    z = (n * (1.0 - E2) + h) * s
    return np.stack([x, y, z], axis=-1)


def ecef_to_lla(xyz):
    xyz = np.asarray(xyz, dtype=np.float64)
    x, y, z = xyz[..., 0], xyz[..., 1], xyz[..., 2]
    p = np.sqrt(x * x + y * y)
    th = np.arctan2(z * A, p * B)
    lon = np.arctan2(y, x)
    lat = np.arctan2(z + EP2 * B * np.sin(th) ** 3, p - E2 * A * np.cos(th) ** 3)
    n = A / np.sqrt(1.0 - E2 * np.sin(lat) ** 2)
    h = p / np.cos(lat) - n
    return np.degrees(lat), np.degrees(lon), h


def enu_basis(lat, lon):
    """Rows are East, North, Up unit vectors expressed in ECEF."""
    la, lo = math.radians(lat), math.radians(lon)
    sl, cl, so, co = math.sin(la), math.cos(la), math.sin(lo), math.cos(lo)
    return np.array([
        [-so, co, 0.0],
        [-sl * co, -sl * so, cl],
        [cl * co, cl * so, sl],
    ], dtype=np.float64)


class LocalFrame(object):
    """East/North/Up tangent frame centred on (lat, lon, h0)."""

    def __init__(self, lat, lon, h0=0.0):
        self.lat, self.lon, self.h0 = float(lat), float(lon), float(h0)
        self.origin = lla_to_ecef(lat, lon, h0)
        self.R = enu_basis(lat, lon)

    # ECEF <-> ENU
    def ecef_to_enu(self, xyz):
        return (np.asarray(xyz, dtype=np.float64) - self.origin) @ self.R.T

    def enu_to_ecef(self, enu):
        return np.asarray(enu, dtype=np.float64) @ self.R + self.origin

    # lat/lon <-> ENU
    def ll_to_enu(self, lat, lon, h=0.0):
        return self.ecef_to_enu(lla_to_ecef(lat, lon, h))

    def enu_to_ll(self, e, n, u=0.0):
        e = np.asarray(e, dtype=np.float64)
        n = np.asarray(n, dtype=np.float64)
        u = np.broadcast_to(np.asarray(u, dtype=np.float64), e.shape)
        return ecef_to_lla(self.enu_to_ecef(np.stack([e, n, u], axis=-1)))

    def enu_to_houdini(self, enu):
        enu = np.asarray(enu, dtype=np.float64)
        return np.stack([enu[..., 0], enu[..., 2], -enu[..., 1]], axis=-1)

    def bbox_ll(self, size_m, margin=0.0):
        """(south, west, north, east) covering the ENU square of side size_m."""
        h = size_m * 0.5 + margin
        e = np.array([-h, h, h, -h])
        n = np.array([-h, -h, h, h])
        lat, lon, _ = self.enu_to_ll(e, n)
        return float(lat.min()), float(lon.min()), float(lat.max()), float(lon.max())


# ---------------------------------------------------------------- Web Mercator
def ll_to_merc(lat, lon):
    lat = np.clip(np.asarray(lat, dtype=np.float64), -85.05112878, 85.05112878)
    x = np.radians(np.asarray(lon, dtype=np.float64)) * MERC_R
    y = np.log(np.tan(math.pi / 4.0 + np.radians(lat) / 2.0)) * MERC_R
    return x, y


def merc_to_ll(x, y):
    lon = np.degrees(np.asarray(x) / MERC_R)
    lat = np.degrees(2.0 * np.arctan(np.exp(np.asarray(y) / MERC_R)) - math.pi / 2.0)
    return lat, lon


def ll_to_tile(lat, lon, z):
    n = 2 ** z
    x = (lon + 180.0) / 360.0 * n
    y = (1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0 * n
    return x, y


def merc_to_global_px(x, y, z, tile_size=256):
    """Mercator metres -> global pixel coords at zoom z (origin top-left)."""
    scale = tile_size * (2 ** z) / (2.0 * MERC_MAX)
    return (np.asarray(x) + MERC_MAX) * scale, (MERC_MAX - np.asarray(y)) * scale


def ground_res(lat, z, tile_size=256):
    return 2.0 * MERC_MAX * math.cos(math.radians(lat)) / (tile_size * 2 ** z)


def zoom_for_res(lat, res_m, tile_size=256, zmax=21):
    for z in range(0, zmax + 1):
        if ground_res(lat, z, tile_size) <= res_m:
            return z
    return zmax


# ---------------------------------------------------------------- sampling
def bilinear(img, px, py):
    """Sample img (H,W[,C]) at float pixel coords (pixel centres at i+0.5)."""
    h, w = img.shape[:2]
    fx = np.clip(np.asarray(px) - 0.5, 0, w - 1.000001)
    fy = np.clip(np.asarray(py) - 0.5, 0, h - 1.000001)
    x0 = np.floor(fx).astype(np.int64)
    y0 = np.floor(fy).astype(np.int64)
    tx = (fx - x0).astype(np.float32)
    ty = (fy - y0).astype(np.float32)
    x1 = np.minimum(x0 + 1, w - 1)
    y1 = np.minimum(y0 + 1, h - 1)
    if img.ndim == 3:
        tx = tx[..., None]
        ty = ty[..., None]
    a = img[y0, x0] * (1 - tx) + img[y0, x1] * tx
    b = img[y1, x0] * (1 - tx) + img[y1, x1] * tx
    return a * (1 - ty) + b * ty


def encode_polyline(points):
    """Google encoded polyline for [(lat, lon), ...]."""
    out = []
    plat = plon = 0
    for lat, lon in points:
        ilat, ilon = int(round(lat * 1e5)), int(round(lon * 1e5))
        for v in (ilat - plat, ilon - plon):
            v = ~(v << 1) if v < 0 else (v << 1)
            while v >= 0x20:
                out.append(chr((0x20 | (v & 0x1f)) + 63))
                v >>= 5
            out.append(chr(v + 63))
        plat, plon = ilat, ilon
    return "".join(out)
