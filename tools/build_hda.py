"""Rebuild the Twinscape HDA from source (houdini/python/earthtwin/build_hda.py).

    hython tools/build_hda.py

Writes houdini/otls/earthtwin_city_builder.hda. Use this to get an HDA saved under your own
Houdini license (the bundled one was saved with Houdini Indie).
"""
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "houdini", "python"))
os.environ.setdefault("TWINSCAPE", REPO)

import hou  # noqa: E402
from earthtwin import build_hda  # noqa: E402

node, path = build_hda.build()
tmp = hou.node("/obj/_earthtwin_build")
if tmp:
    tmp.destroy()
print("HDA written:", path)
