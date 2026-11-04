"""Build houdini/scenes/Twinscape_demo.hip from the bundled sample areas.

    hython tools/make_demo_scene.py

Three areas, all pointing at <repo>/cache:
  twinscape_midtown           open data (USGS lidar terrain, NAIP imagery, OSM 3D buildings), cached
  twinscape_lower_manhattan   open data, cached
  twinscape_midtown_3dtiles   Google Photorealistic 3D Tiles - needs your own API key; Auto Fetch is
                              off so opening the scene never downloads anything
"""
import math
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "houdini", "python"))
os.environ.setdefault("TWINSCAPE", REPO)

import hou  # noqa: E402

if hou.nodeType(hou.sopNodeTypeCategory(), "earthtwin::city_builder::1.0") is None:
    # normally found through the package's HOUDINI_PATH; keep the path variable-relative
    hou.hda.installFile("$TWINSCAPE/houdini/otls/earthtwin_city_builder.hda")
hou.hipFile.clear(suppress_save_prompt=True)
obj = hou.node("/obj")


def area(geo_name, display, **parms):
    g = obj.createNode("geo", geo_name)
    n = g.createNode("earthtwin::city_builder::1.0", "city_builder")
    for k, v in parms.items():
        n.parm(k).set(v)
    n.setDisplayFlag(True)
    n.setRenderFlag(True)
    g.setDisplayFlag(display)
    return g, n


mid, mid_n = area("twinscape_midtown", True, area_name="Midtown", lat=40.7484, lon=-73.9857, size=1000.0)
low, low_n = area("twinscape_lower_manhattan", False, area_name="Lower Manhattan", lat=40.708, lon=-74.0115,
                  size=1200.0)
g3d, g3d_n = area("twinscape_midtown_3dtiles", False, area_name="Midtown 3D Tiles", lat=40.7484, lon=-73.9857,
                  size=1000.0, mode="google3d", g3d_error=6.0, g3d_focus_error=2.5, g3d_focus_radius=150.0,
                  auto_fetch=0)
low.setPosition(mid.position() + hou.Vector2(3, 0))
g3d.setPosition(mid.position() + hou.Vector2(6, 0))

cam = obj.createNode("cam", "demo_cam")
for k, v in dict(resx=1920, resy=1080, focal=28, far=50000).items():
    cam.parm(k).set(v)
p, t = (-700.0, 620.0, 980.0), (0.0, 60.0, 0.0)
d = [t[i] - p[i] for i in range(3)]
cam.parmTuple("t").set(p)
cam.parmTuple("r").set((math.degrees(math.atan2(d[1], math.hypot(d[0], d[2]))),
                        math.degrees(math.atan2(-d[0], -d[2])), 0))
cam.setPosition(mid.position() + hou.Vector2(0, -2))

note = obj.createStickyNote("readme")
note.setText(
    "TWINSCAPE DEMO\n\n"
    "twinscape_midtown / twinscape_lower_manhattan: bundled open-data samples (offline).\n"
    "Select a city_builder node and press 'Open Twinscape Panel' to pick a new place.\n\n"
    "twinscape_midtown_3dtiles: Google Photorealistic 3D Tiles. Add your Maps Platform key\n"
    "(panel > API key, or ~/.twinscape/google_api_key) and press Fetch.\n\n"
    "Unreal: open unreal/TwinscapeUE and use the Twinscape menu. See docs/.")
note.setSize(hou.Vector2(7, 3))
note.setPosition(mid.position() + hou.Vector2(-1, 3))

ok = []
for n in (mid_n, low_n):
    n.cook(force=True)
    geo = n.geometry()
    ok.append((n.parent().name(), len(geo.prims()), n.errors()))
out = os.path.join(REPO, "houdini", "scenes", "Twinscape_demo.hip")
os.makedirs(os.path.dirname(out), exist_ok=True)
hou.hipFile.save(out)
print("saved", hou.hipFile.path())
for row in ok:
    print("  cooked", row)
