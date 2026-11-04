"""HTTP session with retries, polite user agent and an on-disk tile cache."""
import os
import time
import hashlib
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

UA = "Twinscape-Houdini/1.0 (+houdini pipeline)"
_local = threading.local()


def session():
    s = getattr(_local, "s", None)
    if s is None:
        s = requests.Session()
        s.headers.update({"User-Agent": UA})
        adapter = requests.adapters.HTTPAdapter(pool_connections=32, pool_maxsize=32)
        s.mount("https://", adapter)
        s.mount("http://", adapter)
        _local.s = s
    return s


class FetchError(RuntimeError):
    pass


def get(url, params=None, retries=4, timeout=60, method="GET", data=None, json=None,
        ok_404=False, headers=None):
    last = None
    for i in range(retries):
        try:
            r = session().request(method, url, params=params, data=data, json=json,
                                  timeout=timeout, headers=headers)
            if r.status_code == 404 and ok_404:
                return None
            if r.status_code in (429, 500, 502, 503, 504):
                last = FetchError("HTTP %d from %s" % (r.status_code, url.split("?")[0]))
                time.sleep(min(30, 2 ** i + 0.5))
                continue
            if r.status_code >= 400:
                # never echo query strings: they can carry API keys
                raise FetchError("HTTP %d from %s: %s" % (r.status_code, url.split("?")[0],
                                                          r.text[:200]))
            return r
        except requests.RequestException as e:
            last = e
            time.sleep(min(30, 2 ** i + 0.5))
    raise FetchError(str(last))


def cached_get(url, cache_path, params=None, ok_404=False):
    """GET with an on-disk cache (used for public open-data tiles only)."""
    if cache_path and os.path.exists(cache_path) and os.path.getsize(cache_path) > 0:
        with open(cache_path, "rb") as f:
            return f.read()
    r = get(url, params=params, ok_404=ok_404)
    if r is None:
        return None
    if cache_path:
        os.makedirs(os.path.dirname(cache_path), exist_ok=True)
        tmp = cache_path + ".part"
        with open(tmp, "wb") as f:
            f.write(r.content)
        os.replace(tmp, cache_path)
    return r.content


def parallel(fn, items, workers=16, progress=None, label=""):
    """Run fn(item) over items with a thread pool; returns list in input order."""
    out = [None] * len(items)
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(fn, it): i for i, it in enumerate(items)}
        for f in as_completed(futs):
            out[futs[f]] = f.result()
            done += 1
            if progress:
                progress(done / float(max(1, len(items))), "%s %d/%d" % (label, done, len(items)))
    return out


def short_hash(*parts):
    h = hashlib.sha1("|".join(str(p) for p in parts).encode("utf-8")).hexdigest()
    return h[:10]
