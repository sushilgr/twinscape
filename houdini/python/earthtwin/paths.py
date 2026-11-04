"""Where Twinscape lives on disk.

The repository root is taken from $TWINSCAPE (set by the Houdini package that install.py
writes), then the legacy $EARTHTWIN, and otherwise derived from this file's location, so a
plain clone works even before anything is configured.

    <root>/houdini/python/earthtwin/paths.py   ->  <root>
"""
import os

_HERE = os.path.dirname(os.path.abspath(__file__))


def root():
    for k in ("TWINSCAPE", "EARTHTWIN"):
        v = os.environ.get(k, "")
        if v and os.path.isdir(v):
            return os.path.normpath(v)
    up = os.path.dirname(os.path.dirname(_HERE))          # .../houdini  (or legacy root)
    return os.path.dirname(up) if os.path.basename(up) == "houdini" else up


def cache_dir():
    """Downloaded / processed areas. Override with $TWINSCAPE_CACHE."""
    return os.path.normpath(os.environ.get("TWINSCAPE_CACHE") or os.path.join(root(), "cache"))


def otls_dir():
    for c in (os.path.join(root(), "houdini", "otls"), os.path.join(root(), "otls")):
        if os.path.isdir(c):
            return c
    return os.path.join(root(), "houdini", "otls")


def export_dir():
    return os.path.join(root(), "export")


def key_files():
    """Places a saved Google Maps Platform key may live (first one is where the panel saves)."""
    home = os.path.expanduser("~")
    return [os.path.join(home, ".twinscape", "google_api_key"),
            os.path.join(home, ".earthtwin", "google_api_key")]
