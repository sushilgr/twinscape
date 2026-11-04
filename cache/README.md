# cache/

Every area Twinscape builds is cached here, one folder per area. The folder name is the area
name plus a hash of its settings, so the same place and settings never download twice.

| File | What it is |
|---|---|
| `manifest.json` | settings, sources, attribution, ground reference height, stats |
| `height.exr` | terrain heights (metres, relative to the ground at the area centre) |
| `ortho.jpg` | aerial imagery, power-of-two, used as the terrain/roof texture |
| `buildings.bgeo.sc` | building footprints and parts with heights and roof attributes |
| `g3d.bgeo.sc`, `g3d_atlas_<k>.jpg` | Google 3D Tiles mesh and texture atlas pages (Google mode) |
| `_shared/` | procedural facade textures shared by every open-data area |

Two open-data areas are bundled so the demo works offline and without an API key:

* `midtown_d12e301dc4/`: Midtown Manhattan, 1 km, centred on the Empire State Building
* `lower_manhattan_e2f8b1722e/`: Lower Manhattan, 1.2 km, around One World Trade Center

They contain USGS 3DEP elevation and USDA NAIP imagery (public domain) and buildings derived
from OpenStreetMap (© OpenStreetMap contributors, ODbL 1.0) with heights from NYC Open Data.
Everything else in this folder is ignored by git. Google-derived areas must never be
committed or shared (see docs/DATA_AND_LICENSING.md); use **Delete Google Data** in the panel
when you're done with them.
