"""Twinscape demo director (Unreal side): scripted editor-camera flights and a PIE 'autopilot'
that drives Manny through character movement input, so a screen recording shows real-time play
without anyone at the keyboard.

    import earthtwin_demo as d
    d.fly([(0, loc, target), (6, loc2, target2), ...])     # editor viewport camera keyframes (seconds)
    d.autopilot(waypoints, speed=520)                      # call while PIE is running
    d.stop()
"""
import math

import unreal

_cb = {"fly": None, "auto": None}
STATE = {"fly_t": 0.0, "auto_i": 0, "auto_t": 0.0, "done": True, "log": []}


def _unreg(k):
    if _cb[k] is not None:
        try:
            unreal.unregister_slate_post_tick_callback(_cb[k])
        except Exception:
            pass
        _cb[k] = None


def stop():
    _unreg("fly")
    _unreg("auto")
    STATE["done"] = True


def _smooth(t):
    return t * t * (3 - 2 * t)


def _rot(loc, tgt):
    d = [tgt[i] - loc[i] for i in range(3)]
    yaw = math.degrees(math.atan2(d[1], d[0]))
    pitch = math.degrees(math.atan2(d[2], math.hypot(d[0], d[1])))
    return pitch, yaw


# ----------------------------------------------------------------- editor camera flight
def fly(keys):
    """keys: [(time_s, (x,y,z), (tx,ty,tz)), ...] interpolated with smoothstep per segment."""
    _unreg("fly")
    ues = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem)
    STATE["fly_t"] = 0.0
    STATE["done"] = False
    keys = sorted(keys, key=lambda k: k[0])

    def tick(dt):
        STATE["fly_t"] += min(dt, 0.1)
        t = STATE["fly_t"]
        if t >= keys[-1][0]:
            k = keys[-1]
            loc, tgt = k[1], k[2]
            _unreg("fly")
            STATE["done"] = True
        else:
            i = max(j for j in range(len(keys)) if keys[j][0] <= t)
            a, b = keys[i], keys[i + 1]
            u = _smooth((t - a[0]) / max(1e-3, b[0] - a[0]))
            loc = [a[1][j] + (b[1][j] - a[1][j]) * u for j in range(3)]
            tgt = [a[2][j] + (b[2][j] - a[2][j]) * u for j in range(3)]
        p, y = _rot(loc, tgt)
        ues.set_level_viewport_camera_info(unreal.Vector(*loc), unreal.Rotator(0, p, y))

    _cb["fly"] = unreal.register_slate_post_tick_callback(tick)


# ----------------------------------------------------------------- PIE helpers
def pie_world():
    try:
        ws = unreal.EditorLevelLibrary.get_pie_worlds(False)
        return ws[0] if ws else None
    except Exception:
        return None


def manny(world=None):
    world = world or pie_world()
    return unreal.GameplayStatics.get_player_pawn(world, 0) if world else None


def dress_manny(speed=520.0):
    """Show the plain UE mannequin: hide the case-study costume meshes and VFX interactors."""
    p = manny()
    if p is None:
        return False
    unreal.SystemLibrary.execute_console_command(pie_world(), "stat none")
    for c in p.get_components_by_class(unreal.StaticMeshComponent):
        c.set_visibility(False, True)
        c.set_collision_enabled(unreal.CollisionEnabled.NO_COLLISION)
    for c in p.get_components_by_class(unreal.ActorComponent):
        if "SurfaceInteractor" in c.get_class().get_name():
            c.set_active(False)
    mesh = p.get_editor_property("mesh")
    mesh.set_visibility(True, True)
    mesh.set_editor_property("hidden_in_game", False)
    mc = p.get_editor_property("character_movement")
    mc.set_editor_property("max_walk_speed", speed)
    mc.set_editor_property("jump_z_velocity", 520.0)
    mc.set_editor_property("rotation_rate", unreal.Rotator(0, 0, 540))
    mc.set_editor_property("orient_rotation_to_movement", True)
    boom = [c for c in p.get_components_by_class(unreal.SpringArmComponent)]
    if boom:
        boom[0].set_editor_property("target_arm_length", 680.0)
        boom[0].set_editor_property("socket_offset", unreal.Vector(0, 80, 140))
        boom[0].set_editor_property("enable_camera_lag", True)
        boom[0].set_editor_property("camera_lag_speed", 6.0)
    return True


def place_manny(loc, yaw):
    p = manny()
    if p:
        p.set_actor_location_and_rotation(unreal.Vector(*loc), unreal.Rotator(0, 0, yaw), False, True)  # (roll, pitch, yaw)
        ctl = p.get_controller()
        if ctl:
            ctl.set_control_rotation(unreal.Rotator(0, -12, yaw))


def autopilot(waypoints, look_pitch=-12.0, cam_lead=0.0):
    """waypoints: [(x, y, action), ...] action in {None, 'jump', 'look:<yaw offset>', 'wait:<s>'}.
    Drives AddMovementInput each frame toward the next waypoint; the control rotation eases
    in behind the movement so the third-person camera follows."""
    _unreg("auto")
    STATE.update(auto_i=0, auto_t=0.0, done=False, wait=0.0, yawoff=0.0, pitch=look_pitch, slow=0.0)

    def tick(dt):
        w = pie_world()
        p = manny(w)
        if p is None:
            return
        i = STATE["auto_i"]
        if i >= len(waypoints):
            _unreg("auto")
            STATE["done"] = True
            return
        if STATE["wait"] > 0:
            STATE["wait"] -= dt
            return
        x, y, act = waypoints[i]
        loc = p.get_actor_location()
        dx, dy = x - loc.x, y - loc.y
        dist = math.hypot(dx, dy)
        if dist < 180:
            STATE["auto_i"] += 1
            if act == "jump":
                p.jump()
            elif act and act.startswith("wait:"):
                STATE["wait"] = float(act.split(":")[1])
            elif act and act.startswith("look:"):
                parts = act.split(":")
                STATE["yawoff"] = float(parts[1])
                STATE["pitch"] = float(parts[2]) if len(parts) > 2 else look_pitch
            return
        d = unreal.Vector(dx / dist, dy / dist, 0)
        p.add_movement_input(d, 1.0, False)
        # photogrammetry streets have cars / kerbs baked into the mesh: hop if we snag
        v = p.get_velocity()
        STATE["slow"] = STATE["slow"] + dt if math.hypot(v.x, v.y) < 80 else 0.0
        if STATE["slow"] > 0.5:
            p.jump()
            STATE["slow"] = 0.0
        ctl = p.get_controller()
        if ctl:
            cur = ctl.get_control_rotation()
            want = math.degrees(math.atan2(dy, dx)) + STATE["yawoff"]
            dyaw = ((want - cur.yaw + 540) % 360) - 180
            k = min(1.0, dt * 2.0)
            kp = min(1.0, dt * 1.2)
            ctl.set_control_rotation(unreal.Rotator(0, cur.pitch + (STATE["pitch"] - cur.pitch) * kp, cur.yaw + dyaw * k))

    _cb["auto"] = unreal.register_slate_post_tick_callback(tick)


def fifth_avenue_path(s_off=81.0, t0=-150.0, t1=170.0, bearing=29.0):
    """Midtown demo route: north up Fifth Avenue past the Empire State Building (local metres ->
    Unreal cm; Houdini x=E, z=S maps to Unreal X, Y)."""
    th = math.radians(bearing)
    ave = (math.sin(th), -math.cos(th))
    st = (math.cos(th), math.sin(th))

    def P(t, act=None):
        x = t * ave[0] + s_off * st[0]
        z = t * ave[1] + s_off * st[1]
        return (x * 100.0, z * 100.0, act)
    yaw = math.degrees(math.atan2(ave[1], ave[0]))
    pts = [P(t0 + 15), P(-90), P(-55, "look:-75:28"), P(-5), P(20, "look:0:-12"), P(35, "jump"),
           P(70), P(95, "look:40:5"), P(120, "look:0:-12"), P(135, "jump"), P(t1)]
    return P(t0)[:2], yaw, pts
