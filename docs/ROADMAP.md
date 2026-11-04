# Roadmap

## Where it stands

Twinscape reliably turns a place into an explorable Unreal level. From above, with Google Photorealistic 3D Tiles, the results are convincing: rooftops, facades and the street grid all read as the real city. With open data the result is cleaner and fully shareable, but stylised: lidar terrain, aerial imagery and procedural buildings with real roof shapes.

## 1. A street-level layer

At street level the results don't yet work as a "playable" environment. Google's finest tiles in Manhattan carry about 2 m of detail, which isn't enough at human scale. Cars, trees and kerbs come out as lumpy shapes, and road textures blur up close. Twinscape already reaches those finest tiles near the focus point, packs their textures at full resolution and adds a subtle asphalt micro-detail, but the source data sets the ceiling.

The plan is to keep Google's tiles for the big picture and blend in a ground layer rebuilt from open data:

| Data | What it gives | Use |
|---|---|---|
| City lidar point clouds (NYC 2017 topobathymetric lidar, USGS 3DEP point clouds) | ~10 cm ground and kerb detail | true road, sidewalk and kerb surfaces, cut and blended under the Google mesh |
| City planimetrics (e.g. NYC Planimetric Database) | surveyed outlines of sidewalks, kerbs, medians, plazas, parking | clean geometry and material zones for the ground |
| Tree inventories (e.g. NYC Street Tree Census) | position, species and trunk size of every street tree | real tree assets in place of the lumpy canopies |
| OpenStreetMap street furniture | benches, lamps, hydrants, bus stops, crossings, lane tags | placement points for a library of street assets |
| Open street-level imagery (Mapillary, Panoramax) | ground-level photos | reference, or photogrammetry / Gaussian splats of key frontages |

Road markings, materials, signage and parked cars would be generated procedurally in Houdini from these datasets. Google geometry below about 3 m would fade out, leaving the photographed buildings above and a crisp, walkable street layer below.

## 2. OpenUSD as the master format

The HDA already exports a USD stage. The next step is to make USD the backbone: Houdini would write each captured area as a **layered USD stage**, with Google tiles, the rebuilt street layer and live data in separate layers that can be updated independently.

Why this is a valid alternative to the Houdini Engine workflow:

* **One asset, many tools.** The same stage opens in Unreal, NVIDIA Omniverse, Isaac Sim, Blender, Maya and Houdini, with no live Houdini Engine session.
* **Non-destructive updates.** Refresh the tiles, add street props or attach sensor feeds without overwriting anyone's work.
* **Level of detail as data.** Variants and payloads can hold coarse, detailed and street-level versions of an area, loaded on demand.

The trade-off: the Unreal-specific setup (Nanite, collision, the photo material, virtual textures) moves from Houdini Engine into a short import script. Practical next steps: one material per atlas page in the export, an unlit/emissive material for photogrammetry, a fast array-based writer, and geospatial metadata on the stage root.

## 3. Omniverse and Isaac Sim

* **NVIDIA Omniverse:** open the stage directly for collaborative review of a digital twin, and attach live data such as traffic, sensors or energy use on top. This is the natural home for city-scale twins.
* **Isaac Sim:** the same stage, converted to Z-up and given physics collision and semantic class labels (building, road, sidewalk, vegetation), becomes a test environment for robotics and drone simulation and for synthetic training data. This is where the street-level layer matters most: a sidewalk robot's sensors treat lumpy cars and kerbs as real obstacles, so Google tiles alone suit aerial and drone scenarios best.

## Smaller items

* Courtyards and holes in OSM footprints (inner rings).
* Per-area HLODs for very large captures.
* A TOPs network to batch-build a grid of areas.
* Rename the internal `earthtwin` identifiers to `twinscape`, with a migration for existing scenes.
* Test and document Windows and Linux.
