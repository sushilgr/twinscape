"""Twinscape (scripts: earthtwin_*) - Unreal Engine side (UE 5.8, Houdini Engine 3.x).

Run inside the Unreal Editor Python environment.

    import earthtwin_unreal as et
    et.build_area(lat=40.708, lon=-74.0115, size=1200, name="Lower Manhattan")   # full pipeline
    # ...or step by step:
    et.prepare_shared(); et.prepare_area(area_dir); et.instantiate(...); et.bake(); et.finalize(); et.setup_level()

Pipeline: Houdini Engine cooks the Twinscape HDA (which fetches/caches data on
its own), meshes reference material instances created here via the
`unreal_material` attribute, outputs are baked to static meshes, then every mesh
gets Nanite + complex-as-simple collision and the level gets a physically based
sky / sun / fog / post setup tuned for a city-scale scene.
"""
import json
import math
import os

import unreal

HOME = os.path.expanduser("~")


def _repo_root():
    """$TWINSCAPE, legacy $EARTHTWIN, else derived from this file (<repo>/unreal/scripts)."""
    for k in ("TWINSCAPE", "EARTHTWIN"):
        v = os.environ.get(k, "")
        if v and os.path.isdir(v):
            return v
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.normpath(os.path.join(here, "..", ".."))


ET_ROOT = _repo_root()
CACHE = os.environ.get("TWINSCAPE_CACHE") or os.path.join(ET_ROOT, "cache")
HDA_FILE = next((p for p in (os.path.join(ET_ROOT, "houdini", "otls", "earthtwin_city_builder.hda"),
                             os.path.join(ET_ROOT, "otls", "earthtwin_city_builder.hda")) if os.path.exists(p)),
                os.path.join(ET_ROOT, "houdini", "otls", "earthtwin_city_builder.hda"))
ROOT = "/Game/Twinscape"
SHARED = ROOT + "/Shared"
MASTERS = ROOT + "/Materials"

PHOTO_NITS = 20000.0   # unlit photogrammetry brightness for a 75,000 lux sun + sky
AT = unreal.AssetToolsHelpers.get_asset_tools()
EAL = unreal.EditorAssetLibrary
MEL = unreal.MaterialEditingLibrary
_state = {"wrapper": None, "cooked": 0, "baked": False, "area_dir": None, "errors": [], "finalize": {}}


def log(*a):
    unreal.log("[Twinscape] " + " ".join(str(x) for x in a))


# ===================================================================== assets
def _load_or_create(name, path, cls, factory):
    full = "%s/%s" % (path, name)
    if EAL.does_asset_exist(full):
        return EAL.load_asset(full)
    EAL.make_directory(path)
    return AT.create_asset(name, path, cls, factory)


def import_texture(file_path, dest_path, name, srgb=True, vt=False, normal=False, mask=False):
    full = "%s/%s" % (dest_path, name)
    task = unreal.AssetImportTask()
    task.set_editor_property("filename", file_path)
    task.set_editor_property("destination_path", dest_path)
    task.set_editor_property("destination_name", name)
    task.set_editor_property("automated", True)
    task.set_editor_property("replace_existing", True)
    task.set_editor_property("save", False)
    AT.import_asset_tasks([task])
    tex = EAL.load_asset(full)
    if tex is None:
        raise RuntimeError("texture import failed: " + file_path)
    tex.set_editor_property("srgb", bool(srgb))
    if mask:
        tex.set_editor_property("compression_settings", unreal.TextureCompressionSettings.TC_MASKS)
    tex.set_editor_property("lod_group", unreal.TextureGroup.TEXTUREGROUP_WORLD)
    tex.set_editor_property("virtual_texture_streaming", bool(vt))
    tex.set_editor_property("mip_gen_settings", unreal.TextureMipGenSettings.TMGS_SHARPEN4)
    EAL.save_loaded_asset(tex)
    return tex


def _expr(mat, cls, x, y, **props):
    e = MEL.create_material_expression(mat, cls, x, y)
    for k, v in props.items():
        e.set_editor_property(k, v)
    return e


def _tex_param(mat, name, x, y, vt=False, mask=False, default=None):
    e = _expr(mat, unreal.MaterialExpressionTextureSampleParameter2D, x, y, parameter_name=name)
    if default:
        e.set_editor_property("texture", EAL.load_asset(default))
    st = unreal.MaterialSamplerType
    e.set_editor_property("sampler_type", (st.SAMPLERTYPE_VIRTUAL_COLOR if vt else
                                           (st.SAMPLERTYPE_MASKS if mask else st.SAMPLERTYPE_COLOR)))
    return e


def _scalar(mat, name, v, x, y):
    return _expr(mat, unreal.MaterialExpressionScalarParameter, x, y, parameter_name=name, default_value=v)


def _vector(mat, name, rgb, x, y):
    return _expr(mat, unreal.MaterialExpressionVectorParameter, x, y, parameter_name=name,
                 default_value=unreal.LinearColor(rgb[0], rgb[1], rgb[2], 1.0))


def _fresh_material(name):
    full = "%s/%s" % (MASTERS, name)
    if EAL.does_asset_exist(full):   # rebuild in place so instances keep their parent
        m = EAL.load_asset(full)
        MEL.delete_all_material_expressions(m)
        return m
    EAL.make_directory(MASTERS)
    return AT.create_asset(name, MASTERS, unreal.Material, unreal.MaterialFactoryNew())


def _street_detail(m, x, y):
    """-> expression: 1 + (detail - 0.5) * 2 * strength * flat * fade (two world-aligned scales)."""
    E = unreal
    wp = _expr(m, E.MaterialExpressionWorldPosition, x, y)
    xy = _expr(m, E.MaterialExpressionComponentMask, x + 200, y, r=True, g=True, b=False, a=False)
    MEL.connect_material_expressions(wp, "", xy, "")
    samples = []
    for i, scale in enumerate((1.0 / 160.0, 1.0 / 45.0)):
        uv = _expr(m, E.MaterialExpressionMultiply, x + 400, y + i * 220)
        MEL.connect_material_expressions(xy, "", uv, "A")
        c = _expr(m, E.MaterialExpressionConstant, x + 250, y + 100 + i * 220, r=scale)
        MEL.connect_material_expressions(c, "", uv, "B")
        t = _expr(m, E.MaterialExpressionTextureSampleParameter2D, x + 600, y + i * 220,
                  parameter_name="DetailTex%d" % i, texture=EAL.load_asset(SHARED + "/Textures/T_ET_StreetDetail"),
                  sampler_type=E.MaterialSamplerType.SAMPLERTYPE_LINEAR_GRAYSCALE)
        MEL.connect_material_expressions(uv, "", t, "UVs")
        samples.append(t)
    avg = _expr(m, E.MaterialExpressionAdd, x + 850, y)
    MEL.connect_material_expressions(samples[0], "R", avg, "A")
    MEL.connect_material_expressions(samples[1], "R", avg, "B")
    cen = _expr(m, E.MaterialExpressionSubtract, x + 1000, y, const_b=1.0)      # (a+b) - 1 = 2*(mean-0.5)
    MEL.connect_material_expressions(avg, "", cen, "A")
    # flat-ness from the world normal: 0 on walls, 1 on roads / pavements / roofs
    nrm = _expr(m, E.MaterialExpressionVertexNormalWS, x + 600, y + 500)
    nz = _expr(m, E.MaterialExpressionComponentMask, x + 800, y + 500, r=False, g=False, b=True, a=False)
    MEL.connect_material_expressions(nrm, "", nz, "")
    nzs = _expr(m, E.MaterialExpressionSubtract, x + 950, y + 500, const_b=0.75)
    MEL.connect_material_expressions(nz, "", nzs, "A")
    nzm = _expr(m, E.MaterialExpressionMultiply, x + 1100, y + 500, const_b=6.0)
    MEL.connect_material_expressions(nzs, "", nzm, "A")
    flat = _expr(m, E.MaterialExpressionSaturate, x + 1250, y + 500)
    MEL.connect_material_expressions(nzm, "", flat, "")
    # distance fade (pixel depth in cm)
    pd = _expr(m, E.MaterialExpressionPixelDepth, x + 600, y + 700)
    fadep = _scalar(m, "DetailFade", 4500.0, x + 600, y + 800)
    dv = _expr(m, E.MaterialExpressionDivide, x + 850, y + 700)
    MEL.connect_material_expressions(pd, "", dv, "A")
    MEL.connect_material_expressions(fadep, "", dv, "B")
    om = _expr(m, E.MaterialExpressionOneMinus, x + 1000, y + 700)
    MEL.connect_material_expressions(dv, "", om, "")
    fade = _expr(m, E.MaterialExpressionSaturate, x + 1150, y + 700)
    MEL.connect_material_expressions(om, "", fade, "")
    strength = _scalar(m, "DetailStrength", 0.35, x + 1150, y + 850)
    k1 = _expr(m, E.MaterialExpressionMultiply, x + 1400, y + 500)
    MEL.connect_material_expressions(flat, "", k1, "A")
    MEL.connect_material_expressions(fade, "", k1, "B")
    k2 = _expr(m, E.MaterialExpressionMultiply, x + 1550, y + 600)
    MEL.connect_material_expressions(k1, "", k2, "A")
    MEL.connect_material_expressions(strength, "", k2, "B")
    d = _expr(m, E.MaterialExpressionMultiply, x + 1700, y + 300)
    MEL.connect_material_expressions(cen, "", d, "A")
    MEL.connect_material_expressions(k2, "", d, "B")
    one = _expr(m, E.MaterialExpressionAdd, x + 1850, y + 300, const_b=1.0)
    MEL.connect_material_expressions(d, "", one, "A")
    return one


def build_master_materials(force=False):
    """Three lean masters: ortho (VT), facade (texture + glass mask), water."""
    P = unreal.MaterialProperty
    made = []
    # ---------------- ortho / terrain / roofs
    if force or not EAL.does_asset_exist(MASTERS + "/M_ET_Ortho"):
        m = _fresh_material("M_ET_Ortho")
        tex = _tex_param(m, "BaseColor", -700, 0, vt=True, default=SHARED + "/Textures/T_DefaultVT")
        tint = _vector(m, "Tint", (1, 1, 1), -700, -200)
        bright = _scalar(m, "Brightness", 1.0, -700, 250)
        mul = _expr(m, unreal.MaterialExpressionMultiply, -400, 0)
        MEL.connect_material_expressions(tex, "RGB", mul, "A")
        MEL.connect_material_expressions(tint, "", mul, "B")
        mul2 = _expr(m, unreal.MaterialExpressionMultiply, -250, 0)
        MEL.connect_material_expressions(mul, "", mul2, "A")
        MEL.connect_material_expressions(bright, "", mul2, "B")
        MEL.connect_material_property(mul2, "", P.MP_BASE_COLOR)
        MEL.connect_material_property(_scalar(m, "Roughness", 0.85, -400, 300), "", P.MP_ROUGHNESS)
        MEL.connect_material_property(_scalar(m, "Specular", 0.35, -400, 400), "", P.MP_SPECULAR)
        made.append(m)
    # ---------------- facade
    if force or not EAL.does_asset_exist(MASTERS + "/M_ET_Facade"):
        m = _fresh_material("M_ET_Facade")
        tex = _tex_param(m, "BaseColor", -900, -100, default=SHARED + "/Textures/T_Facade_0")
        msk = _tex_param(m, "GlassMask", -900, 250, mask=True, default=SHARED + "/Textures/T_Facade_0_Mask")
        tint = _vector(m, "Tint", (1, 1, 1), -900, -350)
        mul = _expr(m, unreal.MaterialExpressionMultiply, -500, -100)
        MEL.connect_material_expressions(tex, "RGB", mul, "A")
        MEL.connect_material_expressions(tint, "", mul, "B")
        # OSM building:colour arrives as vertex colour (alpha = strength); recolour wall, keep glass
        vc = _expr(m, unreal.MaterialExpressionVertexColor, -900, -600)
        lum = _expr(m, unreal.MaterialExpressionDotProduct, -650, -500)
        MEL.connect_material_expressions(tex, "RGB", lum, "A")
        MEL.connect_material_expressions(_expr(m, unreal.MaterialExpressionConstant3Vector, -900, -450,
                                               constant=unreal.LinearColor(0.3, 0.59, 0.11, 1)), "", lum, "B")
        gain = _expr(m, unreal.MaterialExpressionMultiply, -500, -500)
        MEL.connect_material_expressions(lum, "", gain, "A")
        MEL.connect_material_expressions(_scalar(m, "ColourGain", 1.8, -650, -400), "", gain, "B")
        col = _expr(m, unreal.MaterialExpressionMultiply, -350, -550)
        MEL.connect_material_expressions(vc, "", col, "A")
        MEL.connect_material_expressions(gain, "", col, "B")
        inv = _expr(m, unreal.MaterialExpressionOneMinus, -650, -250)
        MEL.connect_material_expressions(msk, "R", inv, "")
        # tint amount: 0 for white (untagged) vertex colour, ramps to 1 as the colour departs from white
        flag = _expr(m, unreal.MaterialExpressionDistance, -900, -300)
        MEL.connect_material_expressions(vc, "", flag, "A")
        MEL.connect_material_expressions(_expr(m, unreal.MaterialExpressionConstant3Vector, -1100, -250,
                                               constant=unreal.LinearColor(1, 1, 1, 1)), "", flag, "B")
        fs = _expr(m, unreal.MaterialExpressionMultiply, -800, -300)
        MEL.connect_material_expressions(flag, "", fs, "A")
        MEL.connect_material_expressions(_expr(m, unreal.MaterialExpressionConstant, -950, -200, r=40.0), "", fs, "B")
        sat = _expr(m, unreal.MaterialExpressionSaturate, -700, -300)
        MEL.connect_material_expressions(fs, "", sat, "")
        amt = _expr(m, unreal.MaterialExpressionMultiply, -500, -250)
        MEL.connect_material_expressions(sat, "", amt, "A")
        MEL.connect_material_expressions(inv, "", amt, "B")
        blend = _expr(m, unreal.MaterialExpressionLinearInterpolate, -250, -200)
        MEL.connect_material_expressions(mul, "", blend, "A")
        MEL.connect_material_expressions(col, "", blend, "B")
        MEL.connect_material_expressions(amt, "", blend, "Alpha")
        MEL.connect_material_property(blend, "", P.MP_BASE_COLOR)
        lr = _expr(m, unreal.MaterialExpressionLinearInterpolate, -400, 250)
        MEL.connect_material_expressions(_scalar(m, "WallRoughness", 0.75, -650, 200), "", lr, "A")
        MEL.connect_material_expressions(_scalar(m, "GlassRoughness", 0.06, -650, 300), "", lr, "B")
        MEL.connect_material_expressions(msk, "R", lr, "Alpha")
        MEL.connect_material_property(lr, "", P.MP_ROUGHNESS)
        ls = _expr(m, unreal.MaterialExpressionLinearInterpolate, -400, 450)
        MEL.connect_material_expressions(_scalar(m, "WallSpecular", 0.4, -650, 420), "", ls, "A")
        MEL.connect_material_expressions(_scalar(m, "GlassSpecular", 1.0, -650, 500), "", ls, "B")
        MEL.connect_material_expressions(msk, "R", ls, "Alpha")
        MEL.connect_material_property(ls, "", P.MP_SPECULAR)
        made.append(m)
    # ---------------- photogrammetry (Google 3D Tiles: KHR_materials_unlit, lighting baked in)
    if force or not EAL.does_asset_exist(MASTERS + "/M_ET_Photo"):
        m = _fresh_material("M_ET_Photo")
        m.set_editor_property("shading_model", unreal.MaterialShadingModel.MSM_UNLIT)
        tex = _tex_param(m, "BaseColor", -900, 0, vt=True, default=SHARED + "/Textures/T_DefaultVT")
        # emissive must match what a sunlit surface reflects under the physical (lux) daylight
        # exposure used by setup_level: ~albedo * (sun + sky illuminance) / pi
        bright = _scalar(m, "Brightness", PHOTO_NITS, -900, 250)
        # mild grade: Google's capture tints asphalt magenta and tiles differ slightly in
        # white balance; a little desaturation reads more natural under ACES
        desat = _expr(m, unreal.MaterialExpressionDesaturation, -700, 0)
        MEL.connect_material_expressions(tex, "RGB", desat, "")
        MEL.connect_material_expressions(_scalar(m, "Desaturation", 0.2, -900, 150), "", desat, "Fraction")
        mul = _expr(m, unreal.MaterialExpressionMultiply, -500, 0)
        MEL.connect_material_expressions(desat, "", mul, "A")
        MEL.connect_material_expressions(bright, "", mul, "B")
        # Street micro-detail. Google's finest tiles carry roughly 10-20 cm per texel on the
        # ground, which smears at eye height. A tiling, mean-neutral luminance detail map is
        # multiplied in on near-horizontal surfaces only and fades out by ~DetailFade cm, so
        # the photo colours stay Google's while asphalt / pavement read crisp under Manny's feet.
        fin = _street_detail(m, -1500, 450)
        out = _expr(m, unreal.MaterialExpressionMultiply, -250, 0)
        MEL.connect_material_expressions(mul, "", out, "A")
        MEL.connect_material_expressions(fin, "", out, "B")
        MEL.connect_material_property(out, "", P.MP_EMISSIVE_COLOR)
        made.append(m)
    # ---------------- water
    if force or not EAL.does_asset_exist(MASTERS + "/M_ET_Water"):
        m = _fresh_material("M_ET_Water")
        MEL.connect_material_property(_vector(m, "Color", (0.012, 0.03, 0.035), -400, 0), "", P.MP_BASE_COLOR)
        MEL.connect_material_property(_scalar(m, "Roughness", 0.04, -400, 150), "", P.MP_ROUGHNESS)
        MEL.connect_material_property(_scalar(m, "Specular", 0.6, -400, 250), "", P.MP_SPECULAR)
        # gentle large-scale normal variation from world-space noise
        wp = _expr(m, unreal.MaterialExpressionWorldPosition, -1100, 400)
        sc = _expr(m, unreal.MaterialExpressionMultiply, -900, 400)
        MEL.connect_material_expressions(wp, "", sc, "A")
        MEL.connect_material_expressions(_scalar(m, "WaveScale", 0.0015, -1100, 520), "", sc, "B")
        nz = _expr(m, unreal.MaterialExpressionNoise, -700, 400, scale=1.0, levels=4,
                   output_min=-1.0, output_max=1.0)
        MEL.connect_material_expressions(sc, "", nz, "Position")
        mk = _expr(m, unreal.MaterialExpressionMultiply, -500, 400)
        MEL.connect_material_expressions(nz, "", mk, "A")
        MEL.connect_material_expressions(_scalar(m, "WaveStrength", 0.08, -700, 560), "", mk, "B")
        app = _expr(m, unreal.MaterialExpressionAppendVector, -350, 400)
        MEL.connect_material_expressions(mk, "", app, "A")
        MEL.connect_material_expressions(mk, "", app, "B")
        app2 = _expr(m, unreal.MaterialExpressionAppendVector, -200, 400)
        MEL.connect_material_expressions(app, "", app2, "A")
        MEL.connect_material_expressions(_expr(m, unreal.MaterialExpressionConstant, -350, 520, r=1.0), "", app2, "B")
        MEL.connect_material_property(app2, "", P.MP_NORMAL)
        made.append(m)
    for m in made:
        MEL.recompile_material(m)
        EAL.save_loaded_asset(m)
    return len(made)


def _mi(name, path, parent, textures=None, scalars=None):
    mi = _load_or_create(name, path, unreal.MaterialInstanceConstant, unreal.MaterialInstanceConstantFactoryNew())
    MEL.set_material_instance_parent(mi, EAL.load_asset(parent))
    for k, v in (textures or {}).items():
        MEL.set_material_instance_texture_parameter_value(mi, k, v)
    for k, v in (scalars or {}).items():
        MEL.set_material_instance_scalar_parameter_value(mi, k, v)
    MEL.update_material_instance(mi)
    EAL.save_loaded_asset(mi)
    return mi


def import_street_detail():
    """Tiling luminance detail (asphalt aggregate + mottling) used by M_ET_Photo near the camera."""
    full = SHARED + "/Textures/T_ET_StreetDetail"
    if EAL.does_asset_exist(full):
        return EAL.load_asset(full)
    src = os.path.join(os.path.dirname(os.path.abspath(__file__)), "T_ET_StreetDetail.png")
    t = import_texture(src, SHARED + "/Textures", "T_ET_StreetDetail", srgb=False)
    t.set_editor_property("compression_settings", unreal.TextureCompressionSettings.TC_GRAYSCALE)
    t.set_editor_property("mip_gen_settings", unreal.TextureMipGenSettings.TMGS_FROM_TEXTURE_GROUP)
    EAL.save_loaded_asset(t)
    return t


def prepare_shared(force_masters=False):
    shared = os.path.join(CACHE, "_shared")
    rough = {0: 0.8, 1: 0.85, 2: 0.35, 3: 0.6}
    texs = {}
    for k in range(4):
        img = os.path.join(shared, "facade_%d.jpg" % k)
        msk = os.path.join(shared, "facade_%d_mask.png" % k)
        if not os.path.exists(img):
            raise RuntimeError("Facade textures missing - fetch an area in Houdini first (%s)" % img)
        texs[k] = (import_texture(img, SHARED + "/Textures", "T_Facade_%d" % k),
                   import_texture(msk, SHARED + "/Textures", "T_Facade_%d_Mask" % k, srgb=False, mask=True))
    if not EAL.does_asset_exist(SHARED + "/Textures/T_DefaultVT"):
        import_texture(os.path.join(shared, "facade_0.jpg"), SHARED + "/Textures", "T_DefaultVT", vt=True)
    import_street_detail()
    # masters need valid default textures (a parameter without one fails to compile)
    build_master_materials(force=force_masters)
    for k in range(4):
        t, tm = texs[k]
        _mi("MI_Facade_%d" % k, SHARED, MASTERS + "/M_ET_Facade", {"BaseColor": t, "GlassMask": tm},
            {"WallRoughness": rough[k]})
    _mi("MI_Water", SHARED, MASTERS + "/M_ET_Water")


def prepare_area(area_dir):
    with open(os.path.join(area_dir, "manifest.json")) as f:
        man = json.load(f)
    aid = man["area_id"]
    dest = "%s/Areas/%s" % (ROOT, aid)
    t = import_texture(os.path.join(area_dir, "ortho.jpg"), dest + "/Textures", "T_Ortho", vt=True)
    _mi("MI_Ortho", dest, MASTERS + "/M_ET_Ortho", {"BaseColor": t})
    for k, page in enumerate(man.get("g3d_atlas", [])):
        tg = import_texture(os.path.join(area_dir, page), dest + "/Textures", "T_G3D_%d" % k, vt=True)
        _mi("MI_G3D_%d" % k, dest, MASTERS + "/M_ET_Photo", {"BaseColor": tg}, {"Brightness": PHOTO_NITS})
    log("materials ready for", aid)
    return man


def find_area(name=None, lat=None, lon=None, size=None, mode="procedural"):
    best = None
    for d in os.listdir(CACHE) if os.path.isdir(CACHE) else []:
        mp = os.path.join(CACHE, d, "manifest.json")
        if not os.path.exists(mp):
            continue
        with open(mp) as f:
            man = json.load(f)
        p = man["params"]
        if lat is not None and (abs(p["lat"] - lat) > 2e-5 or abs(p["lon"] - lon) > 2e-5):
            continue
        if size is not None and abs(p["size"] - size) > 1e-3:
            continue
        if name and p["name"] != name:
            continue
        if p.get("mode", "procedural") != mode:
            continue
        if best is None or man["created"] > best[1]["created"]:
            best = (os.path.join(CACHE, d), man)
    return best


# ===================================================================== Houdini Engine
def import_hda():
    dest = ROOT + "/HDA"
    full = dest + "/earthtwin_city_builder"
    task = unreal.AssetImportTask()
    task.set_editor_property("filename", HDA_FILE)
    task.set_editor_property("destination_path", dest)
    task.set_editor_property("automated", True)
    task.set_editor_property("replace_existing", True)
    task.set_editor_property("save", True)
    AT.import_asset_tasks([task])
    asset = EAL.load_asset(full)
    if asset is None:
        raise RuntimeError("HDA import failed")
    return asset


def _on_post_process(wrapper):
    _state["cooked"] += 1
    log("cook finished", _state["cooked"])


def _on_post_bake(wrapper, success):
    _state["baked"] = bool(success)
    log("bake finished", success)


def instantiate(lat, lon, size, name, params=None, location=(0, 0, 0)):
    api = unreal.HoudiniPublicAPIBlueprintLib.get_api()
    if not api.is_session_valid():
        api.create_session()
    asset = import_hda()
    old = _state.get("wrapper")
    if old is not None:
        try:
            old.delete_instantiated_asset()
        except Exception:
            pass
    # Twinscape meshes don't need mesh distance fields (Lumen HWRT); skip them while this HDA
    # cooks/bakes, then restore the project's Houdini Engine setting in bake()
    hs = unreal.get_default_object(unreal.HoudiniRuntimeSettings)
    if "df_restore" not in _state:
        _state["df_restore"] = hs.get_editor_property("distance_field_resolution_scale")
    hs.set_editor_property("distance_field_resolution_scale", 0.0)
    xf = unreal.Transform(location=unreal.Vector(*location))
    w = api.instantiate_asset(asset, xf, None, None, True, False)
    _state.update(wrapper=w, cooked=0, baked=False)
    w.on_post_processing_delegate.add_callable(_on_post_process)
    w.on_post_bake_delegate.add_callable(_on_post_bake)
    vals = dict(area_name=name, lat=lat, lon=lon, size=size)
    vals.update(params or {})
    pending = dict(vals)

    def apply(wr):
        # parameters only exist once the asset is instantiated: keep anything that failed
        # (or was set too early) and apply it again after instantiation
        failed = {}
        for k, v in pending.items():
            try:
                if isinstance(v, bool):
                    ok = wr.set_bool_parameter_value(k, v)
                elif isinstance(v, int):
                    ok = wr.set_int_parameter_value(k, v)
                elif isinstance(v, float):
                    ok = wr.set_float_parameter_value(k, v)
                else:
                    ok = wr.set_string_parameter_value(k, str(v))
            except Exception:
                ok = False
            if ok is False:
                failed[k] = v
        _state["param_failures"] = sorted(failed)
        return failed
    w.on_pre_instantiation_delegate.add_callable(apply)
    w.on_post_instantiation_delegate.add_callable(lambda wr: (apply(wr), wr.recook()))
    return w


def status():
    w = _state.get("wrapper")
    out = dict(cooked=_state["cooked"], baked=_state["baked"], finalize=_state.get("finalize"),
               quick=_state.get("quick"))
    if w is not None:
        try:
            out["outputs"] = w.get_num_outputs()
        except Exception:
            pass
    return out


def bake():
    w = _state["wrapper"]
    _state["baked"] = False
    # replace previous bake, drop the temporary HE outputs so nothing is drawn twice
    ok = w.bake_all_outputs_with_settings(unreal.HoudiniEngineBakeOption.TO_ACTOR, True, True, False)
    if "df_restore" in _state:
        unreal.get_default_object(unreal.HoudiniRuntimeSettings).set_editor_property(
            "distance_field_resolution_scale", _state.pop("df_restore"))
    ell = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    for a in ell.get_all_level_actors():
        if isinstance(a, unreal.StaticMeshActor) and a.get_actor_label().startswith("SM_"):
            a.set_folder_path("Twinscape/City")
    return ok


# ===================================================================== post-process
def _sm_subsystem():
    try:
        return unreal.get_editor_subsystem(unreal.StaticMeshEditorSubsystem)
    except Exception:
        return unreal.EditorStaticMeshLibrary


def finalize_async(folder, per_tick=1, on_done=None, **kw):
    """Same as finalize() but processes `per_tick` meshes per editor tick so the editor keeps
    pumping window/OS events. Long synchronous rebuilds on the game thread can crash the macOS
    editor (re-entrant window-move handling)."""
    reg = unreal.AssetRegistryHelpers.get_asset_registry()
    todo = [ad.package_name for ad in reg.get_assets_by_path(folder, recursive=True)
            if str(ad.asset_class_path.asset_name) == "StaticMesh"]
    _state["finalize"] = {"total": len(todo), "done": 0, "running": True}
    holder = {}

    def tick(dt):
        for _ in range(per_tick):
            if not todo:
                unreal.unregister_slate_post_tick_callback(holder["h"])
                _state["finalize"]["running"] = False
                _finalize_actors(folder)
                if on_done:
                    on_done()
                return
            pkg = str(todo.pop(0))
            _finalize_mesh(EAL.load_asset(pkg), **kw)
            _state["finalize"]["done"] += 1

    holder["h"] = unreal.register_slate_post_tick_callback(tick)
    return len(todo)


def _finalize_mesh(sm, nanite=True, collision=True, distance_fields=False):
    """Nanite with full-detail fallback, complex-as-simple collision, no distance field, no lightmap."""
    changed = False
    if nanite:
        ns = sm.get_editor_property("nanite_settings")
        if not ns.enabled:
            ns.enabled = True
            changed = True
        # full-detail fallback: drawn wherever Nanite isn't (this Mac's editor view, ray tracing);
        # decimated fallbacks shard domes and curved footprints
        if ns.get_editor_property("fallback_relative_error") != 0.0:
            try:
                ns.set_editor_property("fallback_target", unreal.NaniteFallbackTarget.RELATIVE_ERROR)
            except Exception:
                pass
            ns.set_editor_property("fallback_relative_error", 0.0)
            changed = True
        if changed:
            sm.set_editor_property("nanite_settings", ns)
    if collision:
        bs = sm.get_editor_property("body_setup")
        if bs and bs.get_editor_property("collision_trace_flag") != unreal.CollisionTraceFlag.CTF_USE_COMPLEX_AS_SIMPLE:
            bs.set_editor_property("collision_trace_flag", unreal.CollisionTraceFlag.CTF_USE_COMPLEX_AS_SIMPLE)
            changed = True
    sub = _sm_subsystem()
    try:
        bset = sub.get_lod_build_settings(sm, 0)
        want = 1.0 if distance_fields else 0.0
        if abs(bset.get_editor_property("distance_field_resolution_scale") - want) > 1e-3:
            bset.set_editor_property("distance_field_resolution_scale", want)
            sub.set_lod_build_settings(sm, 0, bset)
            changed = True
    except Exception as e:
        _state["errors"].append("build settings: %s" % e)
    if sm.get_editor_property("light_map_resolution") != 4:
        sm.set_editor_property("light_map_resolution", 4)
        changed = True
    if changed:
        EAL.save_loaded_asset(sm)
    return changed


def _finalize_actors(folder):
    ell = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    for a in ell.get_all_level_actors():
        if isinstance(a, unreal.StaticMeshActor):
            c = a.static_mesh_component
            m = c.static_mesh
            if m and m.get_path_name().startswith(folder):
                c.set_mobility(unreal.ComponentMobility.STATIC)
                a.set_folder_path("Twinscape/City")


def finalize(folder, nanite=True, collision=True, distance_fields=False):
    """Synchronous version (small areas only; prefer finalize_async in the editor)."""
    reg = unreal.AssetRegistryHelpers.get_asset_registry()
    n = 0
    for ad in reg.get_assets_by_path(folder, recursive=True):
        if str(ad.asset_class_path.asset_name) == "StaticMesh":
            _finalize_mesh(ad.get_asset(), nanite, collision, distance_fields)
            n += 1
    _finalize_actors(folder)
    log("finalized %d meshes" % n)
    return n


# ===================================================================== level
def _spawn(cls, loc=(0, 0, 0), rot=(0, 0, 0), label=None):
    ell = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    a = ell.spawn_actor_from_class(cls, unreal.Vector(*loc), unreal.Rotator(*rot))
    if label:
        a.set_actor_label(label)
    a.set_folder_path("Twinscape/Lighting")
    return a


def _setp(obj, names, val):
    for n in (names if isinstance(names, tuple) else (names,)):
        try:
            obj.set_editor_property(n, val)
            return True
        except Exception:
            continue
    _state["errors"].append("%s.%s" % (obj.__class__.__name__, names))
    return False


def setup_level(map_path=ROOT + "/Maps/Twinscape_City", sun_pitch=-38.0, sun_yaw=-125.0, new_level=True):
    """Photoreal, Lumen-lit level for a city-scale scene."""
    les = unreal.get_editor_subsystem(unreal.LevelEditorSubsystem)
    if new_level:
        EAL.make_directory(ROOT + "/Maps")
        if EAL.does_asset_exist(map_path):
            les.load_level(map_path)
        else:
            les.new_level(map_path)
    ell = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    for a in ell.get_all_level_actors():
        if a.get_folder_path() == "Twinscape/Lighting":
            ell.destroy_actor(a)

    sun = _spawn(unreal.DirectionalLight, (0, 0, 50000), (0, sun_pitch, sun_yaw), "ET_Sun")
    lc = sun.light_component
    _setp(lc, "intensity", 75000.0)  # physical sun (lux)
    _setp(lc, "light_source_angle", 0.5357)
    _setp(lc, "atmosphere_sun_light", True)
    _setp(lc, "mobility", unreal.ComponentMobility.MOVABLE)
    _setp(lc, "cast_cloud_shadows", False)  # ~9 ms GPU on this Mac; clouds still render
    _setp(lc, "light_color", unreal.Color(255, 247, 236, 255))

    sky = _spawn(unreal.SkyAtmosphere, (0, 0, 0), (0, 0, 0), "ET_SkyAtmosphere")
    sk = sky.get_component_by_class(unreal.SkyAtmosphereComponent)
    _setp(sk, "transform_mode", unreal.SkyAtmosphereTransformMode.PLANET_TOP_AT_ABSOLUTE_WORLD_ORIGIN)
    _setp(sk, "aerial_pespective_view_distance_scale", 1.5)

    sl = _spawn(unreal.SkyLight, (0, 0, 2000), (0, 0, 0), "ET_SkyLight")
    slc = sl.light_component
    _setp(slc, "mobility", unreal.ComponentMobility.MOVABLE)
    _setp(slc, "real_time_capture", True)
    _setp(slc, "intensity", 1.0)

    fog = _spawn(unreal.ExponentialHeightFog, (0, 0, -500), (0, 0, 0), "ET_HeightFog")
    fc = fog.component
    _setp(fc, "fog_density", 0.006)
    _setp(fc, "fog_height_falloff", 0.05)
    _setp(fc, "start_distance", 15000.0)
    _setp(fc, ("enable_volumetric_fog", "volumetric_fog"), False)  # perf: aerial perspective does the heavy lifting
    _setp(fc, "fog_inscattering_luminance", unreal.LinearColor(0.35, 0.45, 0.6, 1))

    cl = _spawn(unreal.VolumetricCloud, (0, 0, 0), (0, 0, 0), "ET_Clouds")
    try:
        mat = EAL.load_asset("/Engine/EngineSky/VolumetricClouds/m_SimpleVolumetricCloud_Inst")
        if mat:
            cl.get_component_by_class(unreal.VolumetricCloudComponent).set_editor_property("material", mat)
    except Exception:
        pass

    ppv = _spawn(unreal.PostProcessVolume, (0, 0, 0), (0, 0, 0), "ET_PostProcess")
    _setp(ppv, "unbound", True)
    s = ppv.settings
    for k, v in dict(
        override_auto_exposure_method=True, auto_exposure_method=unreal.AutoExposureMethod.AEM_HISTOGRAM,
        override_auto_exposure_bias=True, auto_exposure_bias=0.3,
        override_auto_exposure_min_brightness=True, auto_exposure_min_brightness=11.0,   # EV100, daylight
        override_auto_exposure_max_brightness=True, auto_exposure_max_brightness=14.5,
        override_dynamic_global_illumination_method=True,
        dynamic_global_illumination_method=unreal.DynamicGlobalIlluminationMethod.LUMEN,
        override_reflection_method=True, reflection_method=unreal.ReflectionMethod.LUMEN,
        override_lumen_scene_lighting_quality=True, lumen_scene_lighting_quality=1.0,
        override_lumen_scene_view_distance=True, lumen_scene_view_distance=200000.0,
        override_lumen_max_trace_distance=True, lumen_max_trace_distance=60000.0,
        override_bloom_intensity=True, bloom_intensity=0.35,
        override_vignette_intensity=True, vignette_intensity=0.25,
        override_film_toe=True, film_toe=0.55,
    ).items():
        _setp(s, k, v)
    _setp(ppv, "settings", s)

    ps = _spawn(unreal.PlayerStart, (0, 0, 3000), (0, 0, 0), "ET_PlayerStart")
    gm = ensure_game_mode()
    if gm is not None:   # clean HUD-less game mode with the mannequin pawn
        unreal.EditorLevelLibrary.get_editor_world().get_world_settings().set_editor_property(
            "default_game_mode", gm)
    return dict(sun=sun.get_name())


PAWN_CANDIDATES = ("/Game/ThirdPerson/Blueprints/BP_ThirdPersonCharacter",
                   "/Game/Characters/Mannequins/Blueprints/BP_ThirdPersonCharacter")


def ensure_game_mode():
    """A game mode whose default pawn is the UE5 mannequin from the Third Person template
    (Add > Add Feature or Content Pack > Third Person). Returns its class, or None when the
    Third Person content isn't in the project (the level then uses the project's default)."""
    for legacy in (ROOT + "/Core/BP_TwinscapeGameMode", "/Game/EarthTwin/Core/BP_EarthTwinGameMode"):
        if EAL.does_asset_exist(legacy):
            return EAL.load_asset(legacy).generated_class()
    pawn = next((p for p in PAWN_CANDIDATES if EAL.does_asset_exist(p)), None)
    if pawn is None:
        log("Third Person content not found - add it to play as the mannequin (see docs/UNREAL.md)")
        return None
    EAL.make_directory(ROOT + "/Core")
    f = unreal.BlueprintFactory()
    f.set_editor_property("parent_class", unreal.GameModeBase)
    bp = AT.create_asset("BP_TwinscapeGameMode", ROOT + "/Core", unreal.Blueprint, f)
    cdo = unreal.get_default_object(bp.generated_class())
    cdo.set_editor_property("default_pawn_class", EAL.load_asset(pawn).generated_class())
    unreal.BlueprintEditorLibrary.compile_blueprint(bp) if hasattr(unreal, "BlueprintEditorLibrary") else None
    EAL.save_loaded_asset(bp)
    return bp.generated_class()


# ---------------------------------------------------------------- performance cvars (editor session)
PERF_CVARS = [
    "r.Nanite.MaxPixelsPerEdge 1",
    "r.Shadow.Virtual.Enable 1",
    "r.Lumen.TraceMeshSDFs 0",           # Nanite city: screen traces + global SDF are enough
    "r.Lumen.ScreenProbeGather.ScreenTraces 1",
    "r.VirtualTextures 1",
    "r.Streaming.PoolSize 3000",
    "r.SkyAtmosphere.FastSkyLUT 1",
    # street-level photogrammetry: roads are seen at grazing angles, so anisotropic filtering
    # and a large enough VT pool decide whether the asphalt reads sharp or smeared
    "r.MaxAnisotropy 16",
    "r.VT.MaxAnisotropy 16",
    "r.VT.PoolSizeScale 2",
    "r.ScreenPercentage 100",
    # bright daylight runs at EV100 up to 14.5: shift Lumen's cached-lighting range to match
    "r.EyeAdaptation.CachedLightingPreExposure 64",
]


def apply_perf_cvars():
    for c in PERF_CVARS:
        unreal.SystemLibrary.execute_console_command(None, c)


def write_perf_config(project_dir=None):
    """Persist city-friendly renderer settings in Config/DefaultEngine.ini (adds missing keys only)."""
    project_dir = project_dir or unreal.Paths.convert_relative_path_to_full(unreal.Paths.project_dir())
    ini = os.path.join(project_dir, "Config", "DefaultEngine.ini")
    with open(ini) as f:
        txt = f.read()
    want = ["r.VirtualTextures=True", "r.Nanite.ProjectEnabled=True", "r.Shadow.Virtual.Enable=1",
            "r.Streaming.PoolSize=3000", "r.MaxAnisotropy=16", "r.VT.MaxAnisotropy=16",
            "r.EyeAdaptation.CachedLightingPreExposure=64"]
    missing = [w for w in want if w.split("=")[0] + "=" not in txt]
    if missing:
        with open(ini, "a") as f:
            f.write("\n; ---- Twinscape city performance defaults\n[/Script/Engine.RendererSettings]\n"
                    + "\n".join(missing) + "\n")
    return ini, missing


def frame_camera(loc, target):
    d = unreal.Vector(target[0] - loc[0], target[1] - loc[1], target[2] - loc[2])
    yaw = math.degrees(math.atan2(d.y, d.x))
    pitch = math.degrees(math.atan2(d.z, math.hypot(d.x, d.y)))
    ues = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem)
    ues.set_level_viewport_camera_info(unreal.Vector(*loc), unreal.Rotator(0, pitch, yaw))


def enu_to_ue(e, n, u):
    """Twinscape local metres -> Unreal cm (X=East, Y=South, Z=Up)."""
    return (e * 100.0, -n * 100.0, u * 100.0)


# ===================================================================== menus
def register_menu():
    menus = unreal.ToolMenus.get()
    main = menus.find_menu("LevelEditor.MainMenu")
    if main is None:
        return
    sub = main.add_sub_menu("Twinscape", "Twinscape", "Twinscape", "Twinscape")
    entries = [
        ("SampleMidtown", "Build sample: Midtown (open data, offline)",
         "import earthtwin_unreal as et; et.quickstart('midtown')"),
        ("SampleLowerManhattan", "Build sample: Lower Manhattan (open data, offline)",
         "import earthtwin_unreal as et; et.quickstart('lower_manhattan')"),
        ("PrepareShared", "Prepare shared materials", "import earthtwin_unreal as et; et.prepare_shared()"),
        ("SetupLevel", "Create lit city level", "import earthtwin_unreal as et; et.setup_level()"),
        ("Finalize", "Optimize baked meshes (Nanite + collision)",
         "import earthtwin_unreal as et; et.finalize('/Game/Twinscape/Areas')"),
        ("PerfCvars", "Apply performance cvars", "import earthtwin_unreal as et; et.apply_perf_cvars()"),
        ("Reload", "Reload Twinscape script", "import importlib, earthtwin_unreal as et; importlib.reload(et)"),
    ]
    for name, label, cmd in entries:
        e = unreal.ToolMenuEntry(name=name, type=unreal.MultiBlockType.MENU_ENTRY)
        e.set_label(label)
        e.set_string_command(unreal.ToolMenuStringCommandType.PYTHON, "", cmd)
        sub.add_menu_entry("Main", e)
    menus.refresh_all_widgets()


# ===================================================================== one-call pipeline
def build_area(lat, lon, size=1200.0, name="Area", params=None, new_level=True):
    """Step 1 of the automated pipeline (async): level + HDA instance.
    HE cooks in the background (auto-fetching data if needed). When status()['cooked'] >= 1,
    call finish_area() to create materials, recook, bake and optimize."""
    prepare_shared()
    setup_level(new_level=new_level)
    area = find_area(name=name, lat=lat, lon=lon, size=size)
    if area:
        prepare_area(area[0])
    _state["pending_area"] = dict(lat=lat, lon=lon, size=size, name=name, had_materials=bool(area))
    return instantiate(lat, lon, size, name, params)


def finish_area():
    """Step 2: ensure area materials exist (recook if they were just created), bake, optimize."""
    pa = _state.get("pending_area") or {}
    area = find_area(name=pa.get("name"), lat=pa.get("lat"), lon=pa.get("lon"), size=pa.get("size"))
    if not area:
        raise RuntimeError("area not cached yet - wait for the cook")
    if not pa.get("had_materials"):
        prepare_area(area[0])
        pa["had_materials"] = True
        _state["cooked"] = 0
        _state["wrapper"].recook()
        return "recooking with materials - call finish_area() again when status()['cooked'] >= 1"
    bake()
    apply_perf_cvars()
    n = finalize_async("%s/Areas/%s" % (ROOT, area[1]["area_id"]),
                       on_done=lambda: unreal.get_editor_subsystem(unreal.LevelEditorSubsystem).save_current_level())
    return "baked; optimizing %d meshes in the background (status()['finalize'])" % n


# ===================================================================== one-click samples
SAMPLES = {
    # cached in <repo>/cache, built from open data only: works offline, no API key
    "midtown": dict(name="Midtown", lat=40.7484, lon=-73.9857, size=1000.0),
    "lower_manhattan": dict(name="Lower Manhattan", lat=40.708, lon=-74.0115, size=1200.0),
}
_watch = {"h": None}


def _poll(cond, then, timeout=1800.0, label=""):
    """Run `then()` on the editor tick once `cond()` is true (keeps the editor responsive)."""
    import time
    t0 = time.time()

    def tick(dt):
        try:
            done = cond()
        except Exception as e:
            done, _state["quick"] = True, "error: %s" % e
        if done or time.time() - t0 > timeout:
            unreal.unregister_slate_post_tick_callback(_watch["h"])
            _watch["h"] = None
            if not done:
                _state["quick"] = "timed out waiting for " + label
                log(_state["quick"])
                return
            then()
    if _watch["h"] is not None:
        unreal.unregister_slate_post_tick_callback(_watch["h"])
    _watch["h"] = unreal.register_slate_post_tick_callback(tick)


def place_player_start(radius_m=180.0, step_m=30.0):
    """Drop the PlayerStart on a street near the centre: the lowest surface hit in a grid
    of down-traces (rooftops are higher, so the minimum is the ground)."""
    world = unreal.EditorLevelLibrary.get_editor_world()
    best = None
    n = int(radius_m // step_m)
    for i in range(-n, n + 1):
        for j in range(-n, n + 1):
            x, y = i * step_m * 100.0, j * step_m * 100.0
            hit = unreal.SystemLibrary.line_trace_single(
                world, unreal.Vector(x, y, 80000.0), unreal.Vector(x, y, -20000.0),
                unreal.TraceTypeQuery.TRACE_TYPE_QUERY1, True, [], unreal.DrawDebugTrace.NONE, True)
            t = hit.to_tuple()
            if t[0]:
                z = t[4].z
                if best is None or z < best[2] - 50 or (abs(z - best[2]) < 50 and abs(x) + abs(y) < abs(best[0]) + abs(best[1])):
                    best = (x, y, z)
    for a in unreal.EditorLevelLibrary.get_all_level_actors():
        if isinstance(a, unreal.PlayerStart) and best:
            a.set_actor_location(unreal.Vector(best[0], best[1], best[2] + 120.0), False, False)
    return best


def quickstart(sample="midtown", **overrides):
    """One click: level, materials, Houdini Engine cook of a cached sample area, bake,
    Nanite + collision, player start on the street, save. Progress: status()['quick']."""
    s = dict(SAMPLES[sample], **overrides)
    slug = s["name"].replace(" ", "")
    prepare_shared()
    setup_level(map_path="%s/Maps/Twinscape_%s" % (ROOT, slug))
    area = find_area(name=s["name"], lat=s["lat"], lon=s["lon"], size=s["size"])
    if area:
        prepare_area(area[0])
    _state["pending_area"] = dict(lat=s["lat"], lon=s["lon"], size=s["size"], name=s["name"], had_materials=bool(area))
    _state["quick"] = "cooking %s in Houdini Engine" % s["name"]
    log(_state["quick"])
    instantiate(s["lat"], s["lon"], s["size"], s["name"], s.get("params"))

    def cooked():
        return _state["cooked"] >= 1

    def after_cook():
        msg = finish_area()
        if msg.startswith("recooking"):
            _state["quick"] = "recooking with area materials"
            _poll(cooked, after_cook, label="recook")
            return
        _state["quick"] = "optimizing meshes (Nanite + collision)"
        log(_state["quick"])
        _poll(lambda: not _state["finalize"].get("running", True), done, label="finalize")

    def done():
        for a in unreal.EditorLevelLibrary.get_all_level_actors():
            if "Houdini" in a.get_class().get_name():      # baked: the live HDA actor is no longer needed
                unreal.EditorLevelLibrary.destroy_actor(a)
        p = place_player_start()
        half = s["size"] * 50.0
        frame_camera((-half * 0.9, half * 1.4, half * 1.1), (0, 0, 0))
        unreal.get_editor_subsystem(unreal.LevelEditorSubsystem).save_current_level()
        _state["quick"] = "done"
        log("%s ready - press Play (player start at %s)" % (s["name"], p))
    _poll(cooked, after_cook, label="cook")
    return "started - watch the Output Log or et.status()['quick']"


# ===================================================================== review captures
def capture(path, loc, target, width=1600, height=900, fov=60.0, ev_fstop=11.0):
    """Render a final-colour still through a SceneCapture2D (works when the editor is in the background)."""
    world = unreal.EditorLevelLibrary.get_editor_world()
    d = unreal.Vector(target[0] - loc[0], target[1] - loc[1], target[2] - loc[2])
    rot = unreal.Rotator(0, math.degrees(math.atan2(d.z, math.hypot(d.x, d.y))), math.degrees(math.atan2(d.y, d.x)))
    ell = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    cap = ell.spawn_actor_from_class(unreal.SceneCapture2D, unreal.Vector(*loc), rot)
    try:
        rt = unreal.RenderingLibrary.create_render_target2d(world, width, height,
                                                            unreal.TextureRenderTargetFormat.RTF_RGBA8_SRGB)
        c = cap.capture_component2d
        c.set_editor_property("texture_target", rt)
        c.set_editor_property("capture_source", unreal.SceneCaptureSource.SCS_FINAL_TONE_CURVE_HDR)
        c.set_editor_property("fov_angle", fov)
        c.set_editor_property("capture_every_frame", False)
        pp = c.get_editor_property("post_process_settings")
        for k, v in dict(override_auto_exposure_method=True, auto_exposure_method=unreal.AutoExposureMethod.AEM_MANUAL,
                         override_auto_exposure_apply_physical_camera_exposure=True,
                         auto_exposure_apply_physical_camera_exposure=True,
                         override_camera_iso=True, camera_iso=100.0,
                         override_camera_shutter_speed=True, camera_shutter_speed=125.0,
                         override_depth_of_field_fstop=True, depth_of_field_fstop=ev_fstop,
                         override_auto_exposure_bias=True, auto_exposure_bias=0.0).items():
            _setp(pp, k, v)
        c.set_editor_property("post_process_settings", pp)
        c.set_editor_property("post_process_blend_weight", 1.0)
        for _ in range(8):  # let Lumen settle a little
            c.capture_scene()
            unreal.RenderingLibrary.read_render_target_pixel(world, rt, 0, 0)   # flushes the render thread
        unreal.RenderingLibrary.export_render_target(world, rt, os.path.dirname(path), os.path.basename(path))
    finally:
        ell.destroy_actor(cap)
    return path
