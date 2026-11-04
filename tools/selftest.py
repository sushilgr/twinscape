"""Check an install without opening the UI: cooks the bundled sample areas offline and
verifies the attributes Unreal relies on.

    hython tools/selftest.py
"""
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "houdini", "python"))
os.environ.setdefault("TWINSCAPE", REPO)

import hou  # noqa: E402

if hou.nodeType(hou.sopNodeTypeCategory(), "earthtwin::city_builder::1.0") is None:
    hou.hda.installFile(os.path.join(REPO, "houdini", "otls", "earthtwin_city_builder.hda"))
fails = []
for name, lat, lon, size in (("Midtown", 40.7484, -73.9857, 1000.0), ("Lower Manhattan", 40.708, -74.0115, 1200.0)):
    g = hou.node("/obj").createNode("geo")
    n = g.createNode("earthtwin::city_builder::1.0")
    n.parm("auto_fetch").set(0)                      # must come from the bundled cache
    for k, v in dict(area_name=name, lat=lat, lon=lon, size=size).items():
        n.parm(k).set(v)
    n.cook(force=True)
    geo = n.geometry()
    if n.errors():
        fails.append("%s: %s" % (name, n.errors()))
        continue
    packed = len(geo.prims())
    inner = n.node("strip_temp").geometry()
    need_prim = ("unreal_material", "chunk")
    missing = [a for a in need_prim if inner.findPrimAttrib(a) is None]
    if not packed or missing or geo.findGlobalAttrib("unreal_bake_folder") is None and \
            n.node("unreal_detail").geometry().findGlobalAttrib("unreal_bake_folder") is None:
        fails.append("%s: packed=%d missing=%s" % (name, packed, missing))
    print("%-16s %4d chunks  %8d polygons  area %s" % (name, packed, len(inner.prims()),
                                                       n.node("load_area").geometry().attribValue("area_id")))
print("PASS" if not fails else "FAIL\n  " + "\n  ".join(fails))
sys.exit(1 if fails else 0)
