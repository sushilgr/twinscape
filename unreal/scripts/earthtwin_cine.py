"""Twinscape cinematics: keyed aerial camera moves as Level Sequences, rendered offline with the
Movie Render Queue (no screen capture, full-quality frames with warm-up for Nanite / virtual
texture streaming).

    import earthtwin_cine as ec
    ec.build_shot("A01_ESB_Orbit", ec.orbit((0, 0, 20000), 60000, 42000, 200, 260, 10, target_z=30000))
    ec.render(["A01_ESB_Orbit"], "/Game/Twinscape/Maps/Twinscape_Midtown", "~/Twinscape/renders")
"""
import math
import os

import unreal

SEQ_DIR = "/Game/Twinscape/Cinematics"
FPS = 30
EAL = unreal.EditorAssetLibrary
AT = unreal.AssetToolsHelpers.get_asset_tools()
RENDER_CVARS = {
    "r.MaxAnisotropy": 16, "r.VT.MaxAnisotropy": 16, "r.VT.PoolSizeScale": 2,
    "r.EyeAdaptation.CachedLightingPreExposure": 64, "r.Nanite.MaxPixelsPerEdge": 1,
    "r.MotionBlur.Amount": 0.25, "r.Streaming.FullyLoadUsedTextures": 1,
    # HiDPI (Retina) game viewports default to a 50% secondary upscale, which the movie capture
    # does not apply: force native resolution so frames fill the whole 1920 x 1080 image
    "r.SecondaryScreenPercentage.GameViewport": 100, "r.ScreenPercentage": 100,
}
_job = {"executor": None, "done": True, "error": None}


# ------------------------------------------------------------------ path helpers (UE cm)
def _ease(u):
    return u * u * (3 - 2 * u)


def _ease_soft(u, k=0.35):
    """Linear in the middle, eased at both ends: smooth start/stop without a slow-motion feel."""
    return (1 - k) * u + k * _ease(u)


def orbit(centre, radius, height, a0, a1, seconds, target_z=None, ease=True, r1=None, h1=None):
    """Circle (optionally spiralling to r1/h1) around centre, looking at (centre.xy, target_z).
    Angles in degrees, 0 = east of the centre, counter-clockwise seen from above."""
    n = int(round(seconds * FPS))
    out = []
    for i in range(n + 1):
        u = i / float(n)
        e = _ease_soft(u) if ease else u
        a = math.radians(a0 + (a1 - a0) * e)
        r = radius + ((r1 if r1 is not None else radius) - radius) * e
        h = height + ((h1 if h1 is not None else height) - height) * e
        loc = (centre[0] + r * math.cos(a), centre[1] - r * math.sin(a), h)
        tgt = (centre[0], centre[1], centre[2] if target_z is None else target_z)
        out.append((loc, tgt))
    return out


def dolly(p0, p1, t0, t1, seconds, ease=True):
    """Straight move p0 -> p1 while the look-at point moves t0 -> t1."""
    n = int(round(seconds * FPS))
    out = []
    for i in range(n + 1):
        u = i / float(n)
        e = _ease_soft(u) if ease else u
        out.append((tuple(p0[k] + (p1[k] - p0[k]) * e for k in range(3)),
                    tuple(t0[k] + (t1[k] - t0[k]) * e for k in range(3))))
    return out


def _rot(loc, tgt):
    d = [tgt[i] - loc[i] for i in range(3)]
    return (math.degrees(math.atan2(d[2], math.hypot(d[0], d[1]))), math.degrees(math.atan2(d[1], d[0])))


def poses(frames):
    """[(loc, tgt)] -> [(loc, pitch, yaw)] with unwrapped yaw."""
    out, last = [], None
    for loc, tgt in frames:
        p, y = _rot(loc, tgt)
        if last is not None:
            y = last + ((y - last + 180.0) % 360.0) - 180.0
        last = y
        out.append((loc, p, y))
    return out


# ------------------------------------------------------------------ sequence
def _camera(label, focal):
    ell = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    for a in ell.get_all_level_actors():
        if a.get_actor_label() == label:
            ell.destroy_actor(a)
    cam = ell.spawn_actor_from_class(unreal.CineCameraActor, unreal.Vector(0, 0, 0), unreal.Rotator(0, 0, 0))
    cam.set_actor_label(label)
    cam.set_folder_path("Twinscape/Cinematics")
    cc = cam.get_cine_camera_component()
    fb = cc.get_editor_property("filmback")
    fb.sensor_width, fb.sensor_height = 36.0, 20.25           # 16:9, no letterbox
    cc.set_editor_property("filmback", fb)
    cc.set_editor_property("current_focal_length", float(focal))
    fs = cc.get_editor_property("focus_settings")
    fs.focus_method = unreal.CameraFocusMethod.DISABLE          # everything sharp from altitude
    cc.set_editor_property("focus_settings", fs)
    return cam


def build_shot(name, frames, focal=28.0):
    """Create / replace LS_<name> with a cine camera keyed on every frame."""
    path = "%s/LS_%s" % (SEQ_DIR, name)
    if EAL.does_asset_exist(path):
        EAL.delete_asset(path)
    seq = AT.create_asset("LS_" + name, SEQ_DIR, unreal.LevelSequence, unreal.LevelSequenceFactoryNew())
    n = len(frames) - 1
    seq.set_display_rate(unreal.FrameRate(FPS, 1))
    seq.set_tick_resolution(unreal.FrameRate(FPS, 1))
    seq.set_playback_start(0)
    seq.set_playback_end(n)
    cam = _camera("CAM_" + name, focal)
    b = seq.add_possessable(cam)
    tr = b.add_track(unreal.MovieScene3DTransformTrack)
    sec = tr.add_section()
    sec.set_range(0, n + 1)
    ch = sec.get_all_channels()   # loc x y z, rot roll pitch yaw, scale x y z
    for f, (loc, p, y) in enumerate(poses(frames)):
        fn = unreal.FrameNumber(f)
        for c, v in zip(ch[:6], (loc[0], loc[1], loc[2], 0.0, p, y)):
            c.add_key(fn, float(v), interpolation=unreal.MovieSceneKeyInterpolation.LINEAR)
    # camera cut bound to this camera
    try:
        cut = seq.add_track(unreal.MovieSceneCameraCutTrack)
    except Exception:
        cut = seq.add_master_track(unreal.MovieSceneCameraCutTrack)
    cs = cut.add_section()
    cs.set_range(0, n + 1)
    bid = unreal.MovieSceneObjectBindingID()
    bid.set_editor_property("guid", b.get_id())
    cs.set_camera_binding_id(seq.get_binding_id(b) if hasattr(seq, "get_binding_id") else bid)
    EAL.save_loaded_asset(seq)
    return path, n + 1


def preview(frames, i=0):
    """Put the editor viewport camera on frame i of a path (for framing checks)."""
    loc, p, y = poses(frames)[i]
    ues = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem)
    ues.set_level_viewport_camera_info(unreal.Vector(*loc), unreal.Rotator(0, p, y))
    return loc, p, y


# ------------------------------------------------------------------ Movie Render Queue
def render(shots, map_path, out_dir, res=(1920, 1080), warmup=120, jpg=True):
    """Queue one MRQ job per shot (LS_<shot>) and render in-editor (PIE executor)."""
    out_dir = os.path.expanduser(out_dir)
    subsys = unreal.get_editor_subsystem(unreal.MoviePipelineQueueSubsystem)
    q = subsys.get_queue()
    q.delete_all_jobs()
    for s in shots:
        job = q.allocate_new_job(unreal.MoviePipelineExecutorJob)
        job.set_editor_property("job_name", s)
        job.sequence = unreal.SoftObjectPath("%s/LS_%s.LS_%s" % (SEQ_DIR, s, s))
        job.map = unreal.SoftObjectPath("%s.%s" % (map_path, map_path.rsplit("/", 1)[1]))
        cfg = job.get_configuration()
        o = cfg.find_or_add_setting_by_class(unreal.MoviePipelineOutputSetting)
        o.output_directory = unreal.DirectoryPath(os.path.join(out_dir, s))
        o.output_resolution = unreal.IntPoint(*res)
        o.file_name_format = "{sequence_name}.{frame_number}"
        o.zero_pad_frame_numbers = 4
        o.override_existing_output = True
        cfg.find_or_add_setting_by_class(unreal.MoviePipelineDeferredPassBase)
        cfg.find_or_add_setting_by_class(unreal.MoviePipelineImageSequenceOutput_JPG if jpg
                                         else unreal.MoviePipelineImageSequenceOutput_PNG)
        aa = cfg.find_or_add_setting_by_class(unreal.MoviePipelineAntiAliasingSetting)
        aa.spatial_sample_count = 1
        aa.temporal_sample_count = 1
        aa.engine_warm_up_count = warmup
        try:
            aa.render_warm_up_count = warmup
            aa.use_camera_cut_for_warm_up = False
        except Exception:
            pass
        cv = cfg.find_or_add_setting_by_class(unreal.MoviePipelineConsoleVariableSetting)
        for k, v in RENDER_CVARS.items():
            try:
                cv.add_or_update_console_variable(k, float(v))
            except Exception:
                pass
    _job.update(done=False, error=None)

    def finished(executor, success):
        _job.update(done=True, error=None if success else "render failed")
    ex = subsys.render_queue_with_executor(unreal.MoviePipelinePIEExecutor)
    ex.on_executor_finished_delegate.add_callable(finished)
    _job["executor"] = ex
    return len(shots)


def render_capture(shots, out_dir, res=(1920, 1080), warmup=90, on_done=None):
    """Same as render() but with the built-in Sequencer movie capture (no Movie Render Queue
    plugin needed). Shots render one after another; each warms up `warmup` frames first so
    Nanite pages and virtual textures are resident before frame 0 is written."""
    out_dir = os.path.expanduser(out_dir)
    todo = list(shots)
    _job.update(done=False, error=None, current=None, finished=[])
    for k, v in RENDER_CVARS.items():
        unreal.SystemLibrary.execute_console_command(None, "%s %s" % (k, v))

    def next_shot(*_):
        if _job.get("current"):
            _job["finished"].append(_job["current"])
        if not todo:
            _job.update(done=True, current=None)
            if on_done:
                on_done()
            return
        s = todo.pop(0)
        _job["current"] = s
        cap = unreal.AutomatedLevelSequenceCapture()
        cap.level_sequence_asset = unreal.SoftObjectPath("%s/LS_%s.LS_%s" % (SEQ_DIR, s, s))
        st = cap.settings
        for k, v in (("output_directory", unreal.DirectoryPath(os.path.join(out_dir, s))),
                     ("output_format", s + ".{frame}"),
                     ("resolution", unreal.CaptureResolution(res[0], res[1])),
                     ("overwrite_existing", True), ("use_relative_frame_numbers", True),
                     ("zero_pad_frame_numbers", 4), ("enable_texture_streaming", True),
                     ("cinematic_mode", True), ("use_custom_frame_rate", False)):
            try:
                st.set_editor_property(k, v)
            except Exception as e:
                _job.setdefault("warnings", []).append("%s: %s" % (k, e))
        cap.settings = st
        cap.set_image_capture_protocol_type(unreal.load_class(None, "/Script/MovieSceneCapture.ImageSequenceProtocol_JPG"))
        cap.warm_up_frame_count = warmup
        cap.delay_before_warm_up = 1.0
        cb = unreal.OnRenderMovieStopped()
        cb.bind_callable(lambda success: next_shot())
        _job["cb"] = cb
        unreal.SequencerTools.render_movie(cap, cb)
    next_shot()
    return len(shots)


def status():
    ex = _job.get("executor")
    out = dict(done=_job["done"], error=_job["error"], current=_job.get("current"),
               finished=_job.get("finished"))
    try:
        out["rendering"] = bool(ex and ex.is_rendering())
    except Exception:
        pass
    return out


def cinematic_atmosphere(water_z=-1400.0):
    """Look used for the aerial explainer: slightly hazier aerial perspective, height fog for
    the far distance and a large water plane at river level so nothing outside the context
    ring reads as empty planet ground."""
    ell = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    for a in ell.get_all_level_actors():
        if isinstance(a, unreal.SkyAtmosphere):
            sk = a.get_component_by_class(unreal.SkyAtmosphereComponent)
            sk.set_editor_property("mie_scattering_scale", 0.008)
            sk.set_editor_property("aerial_pespective_view_distance_scale", 2.5)
        elif isinstance(a, unreal.ExponentialHeightFog):
            fc = a.component
            fc.set_editor_property("fog_density", 0.03)
            fc.set_editor_property("fog_height_falloff", 0.2)
            fc.set_editor_property("start_distance", 150000.0)
            fc.set_editor_property("fog_inscattering_luminance", unreal.LinearColor(0.62, 0.70, 0.80, 1))
        elif a.get_actor_label() == "ET_Horizon_Water":
            ell.destroy_actor(a)
    w = ell.spawn_actor_from_class(unreal.StaticMeshActor, unreal.Vector(0, 0, water_z), unreal.Rotator(0, 0, 0))
    w.set_actor_label("ET_Horizon_Water")
    w.set_folder_path("Twinscape/Lighting")
    c = w.static_mesh_component
    c.set_static_mesh(EAL.load_asset("/Engine/BasicShapes/Plane"))
    c.set_material(0, EAL.load_asset("/Game/Twinscape/Shared/MI_Water"))
    w.set_actor_scale3d(unreal.Vector(40000, 40000, 1))
    c.set_editor_property("cast_shadow", False)
    c.set_collision_enabled(unreal.CollisionEnabled.NO_COLLISION)
    return w
