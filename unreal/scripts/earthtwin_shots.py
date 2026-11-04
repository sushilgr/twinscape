"""Aerial shot list for the Twinscape explainer (Midtown 3D Tiles level, UE cm: X=East, Y=South, Z=Up).

Rules the moves follow so only the convincing parts of the twin are on screen:
  * altitude >= ~250 m and a downward pitch steep enough that the top of frame meets the
    ground inside the 4 km context ring (no horizon, no edge of the data);
  * when the Empire State Building is the subject the camera stays above its spire so the
    whole tower is in frame against the street grid, never against the sky;
  * framing on facades, rooftops and the street grid - street-level objects (cars, trees,
    planters) are a few pixels at these distances;
  * the detailed core (Google's finest tiles within ~250 m of the Empire State Building)
    fills most of the frame; the coarser ring only appears as background.
"""
import math

import earthtwin_cine as ec

FOCAL = 30.0                     # 36 x 20.25 mm filmback: 62 deg horizontal, 37 deg vertical
ESB = (0.0, 0.0)                 # area centre = Empire State Building
HERALD = (-27800.0, -26600.0)    # Macy's Herald Square
SW = (-0.5, 0.866)               # unit vector from the ESB toward the south-west (UE x, y)


SE = (0.5, 0.866)
NW = (-0.866, -0.5)


def _toward(d_m, h_m, pitch, u=SW):
    """Camera d_m metres from the ESB along unit vector u at h_m, looking at the ESB axis."""
    loc = (u[0] * d_m * 100, u[1] * d_m * 100, h_m * 100)
    tgt = (0.0, 0.0, (h_m - d_m * math.tan(math.radians(-pitch))) * 100)
    return loc, tgt


def shots():
    s = {}
    # 1. glide in from the south-east, the Empire State Building rising out of the grid
    (a, at), (b, bt) = _toward(650, 760, -45, SE), _toward(400, 620, -43, SE)
    s["M01_Approach"] = ec.dolly(a, b, at, bt, 9.0)
    # 2. orbit the Empire State Building from above its spire, the core turning beneath it
    s["M02_ESB_Orbit"] = ec.orbit((ESB[0], ESB[1], 0), 42000, 64000, 240, 320, 10.0, target_z=26000, r1=40000)
    # 3. top-down: the street grid and rooftops of the detailed core, slow rotation
    s["M03_Grid"] = ec.orbit((ESB[0], ESB[1], 0), 4000, 105000, 100, 135, 9.0, target_z=0.0,
                             r1=3000, h1=88000)
    # 4. Herald Square: orbit over Macy's and the 34th Street busway
    s["M04_Herald"] = ec.orbit((HERALD[0], HERALD[1], 0), 26000, 38000, 160, 225, 9.0, target_z=2500)
    # 5. crane up the Empire State Building's Art Deco setbacks to the spire
    a = (NW[0] * 22200, NW[1] * 22200, 22000)
    b = (NW[0] * 22200, NW[1] * 22200, 56000)
    s["M05_SpireRise"] = ec.dolly(a, b, (0, 0, 22000 - 22200 * math.tan(math.radians(36))),
                                  (0, 0, 56000 - 22200 * math.tan(math.radians(40))), 9.0)
    # 6. pull back and rise: the whole Midtown twin
    (a, at), (b, bt) = _toward(300, 600, -42), _toward(1000, 1250, -52)
    s["M06_Pullback"] = ec.dolly(a, b, at, bt, 11.0)
    return s


def birdseye(seconds=12.0, h0=760000.0, h1=240000.0, yaw0=-90.0, yaw1=-78.0):
    """Top-down descent over the whole level: the 4 km context ring inside the water plane,
    then the detailed 1 km core filling the frame. Image up = north at yaw -90."""
    n = int(round(seconds * ec.FPS))
    out = []
    for i in range(n + 1):
        u = i / float(n)
        e = ec._ease_soft(u, 0.5)
        h = h0 * (h1 / h0) ** e
        yaw = math.radians(yaw0 + (yaw1 - yaw0) * e)
        loc = (0.0, 0.0, h)
        tgt = (math.cos(yaw) * 10.0, math.sin(yaw) * 10.0, h - 100000.0)
        out.append((loc, tgt))
    return out
