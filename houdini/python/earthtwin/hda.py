"""HDA-side glue: parameter callbacks, fetch-from-node, USD export.

Works in interactive Houdini and inside Houdini Engine (Unreal), because the
Fetch button runs synchronously without any UI.
"""
import json
import os

import hou

from . import ingest

PARM_MAP = dict(name="area_name", lat="lat", lon="lon", size="size", mode="mode",
                terrain_src="terrain_src", imagery_src="imagery_src", buildings_src="buildings_src",
                dem_res="dem_res", ortho_res="ortho_res", max_tex="max_tex", g3d_error="g3d_error",
                g3d_focus_error="g3d_focus_error", g3d_focus_radius="g3d_focus_radius", g3d_hole="g3d_hole",
                default_height="bld_default_height", cache_dir="cache_dir")


def area_dir_expr(node):
    try:
        return ingest.area_dir(_full(node_params(node)))
    except Exception as e:
        return "<error: %s>" % e


def _full(p):
    q = dict(ingest.DEFAULTS)
    q.update({k: v for k, v in p.items() if v is not None and k != "api_key"})
    q["size"] = float(q["size"])
    return q


def info_text(node):
    d = area_dir_expr(node)
    mp = os.path.join(d, "manifest.json")
    if not os.path.exists(mp):
        return "Not fetched yet. Press Fetch (or enable Auto Fetch and cook)."
    with open(mp) as f:
        man = json.load(f)
    g3d = man["params"]["mode"] == "google3d"
    return "%s\nDEM %d x %d (ref %.1f m, %.1f..%.1f)   Ortho %d px @ %.2f m/px\n%s\n%s" % (
        man["area_id"], man["dem_n"], man["dem_n"], man["elev_ref"], man["elev_min"], man["elev_max"],
        man["ortho_px"], man["ortho_res"],
        ("Google tiles %d, %d tris" % (man.get("g3d_tiles", 0), man.get("g3d_triangles", 0))) if g3d
        else ("Buildings: %d (%s)" % (man.get("buildings", 0), man.get("buildings_src_used", ""))),
        " / ".join(man.get("attribution", [])))


def cook_load(sop):
    """Body of the HDA's load_area Python SOP. Ensures data exists (auto fetch),
    then loads footprints / Google mesh and publishes area metadata as detail attribs."""
    hda_node = sop.parent()
    params = node_params(hda_node)
    full = _full(params)
    d = ingest.area_dir(full)
    mp = os.path.join(d, "manifest.json")
    if not os.path.exists(mp):
        if hda_node.evalParm("auto_fetch"):
            ingest.fetch(params)
            hou.hscript("texcache -c")
        else:
            raise hou.NodeWarning("Area not fetched yet - press Fetch on the Twinscape node.")
    with open(mp) as f:
        man = json.load(f)
    geo = sop.geometry()
    geo.clear()
    if full["mode"] == "google3d":
        p = os.path.join(d, "g3d.bgeo.sc")
        if os.path.exists(p):
            geo.loadFromFile(p)
    else:
        p = os.path.join(d, "buildings.bgeo.sc")
        if os.path.exists(p):
            geo.loadFromFile(p)
    slug = hou.text.variableName(full["name"]).strip("_") or "Area"
    vals = dict(area_dir=d, area_id=man["area_id"], area_slug=slug, cache_dir=full["cache_dir"],
                attribution=" / ".join(man.get("attribution", [])))
    for k, v in vals.items():
        geo.addAttrib(hou.attribType.Global, k, "")
        geo.setGlobalAttribValue(k, v)
    for k, v in dict(dem_n=int(man["dem_n"]), g3d_pages=len(man.get("g3d_atlas", []))).items():
        geo.addAttrib(hou.attribType.Global, k, 0)
        geo.setGlobalAttribValue(k, v)
    for k, v in dict(size=float(full["size"]), elev_ref=float(man["elev_ref"]),
                     elev_min=float(man["elev_min"]), lat=float(full["lat"]), lon=float(full["lon"])).items():
        geo.addAttrib(hou.attribType.Global, k, 0.0)
        geo.setGlobalAttribValue(k, v)


def node_params(node):
    p = {}
    for k, parm in PARM_MAP.items():
        pt = node.parm(parm)
        if pt is None:
            continue
        tmpl = pt.parmTemplate()
        if isinstance(tmpl, hou.MenuParmTemplate):
            p[k] = pt.evalAsString()
        else:
            p[k] = pt.eval()
    p["max_tex"] = int(p.get("max_tex", 8192))
    if not p.get("cache_dir"):          # empty = <repo>/cache (resolved here so every process agrees)
        p.pop("cache_dir", None)
    if node.parm("api_key") is not None:
        p["api_key"] = node.parm("api_key").evalAsString()
    return p


def apply_manifest(node, man=None):
    hou.hscript("texcache -c")
    load = node.node("load_area")
    if load is not None:
        load.cook(force=True)
    node.cook(force=True)


def fetch(node, force=False):
    params = node_params(node)
    with hou.InterruptableOperation("Twinscape fetch", long_operation_name="Twinscape: ingesting data",
                                    open_interrupt_dialog=True) as op:
        def prog(f, msg):
            try:
                op.updateLongProgress(max(0.0, min(1.0, f)), msg)
            except hou.OperationInterrupted:
                raise
            except Exception:
                pass
        man = ingest.fetch(params, progress=prog, force=force)
    apply_manifest(node, man)
    return man


# ------------------------------------------------------------------ callbacks
def on_preset(node):
    name = node.parm("preset").evalAsString()
    if name in ingest.PRESETS:
        lat, lon, size = ingest.PRESETS[name]
        with hou.undos.group("Twinscape preset"):
            node.parm("lat").set(lat)
            node.parm("lon").set(lon)
            node.parm("size").set(size)
            node.parm("area_name").set(name.split("(")[0].strip())


def preset_menu():
    out = []
    for k in ingest.PRESETS:
        out += [k, k]
    return out


def on_fetch(kwargs):
    fetch(kwargs["node"], force=bool(kwargs.get("shift")))


def on_open_panel(kwargs):
    from . import ui
    ui.show(kwargs["node"])


def on_open_folder(kwargs):
    d = kwargs["node"].parm("area_dir").evalAsString()
    if d and os.path.isdir(d):
        import subprocess, sys
        subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", d])


def on_purge_google(kwargs):
    gone = ingest.purge_google(_full(node_params(kwargs["node"]))["cache_dir"])
    hou.hscript("texcache -c")
    kwargs["node"].cook(force=True) if kwargs["node"].evalParm("mode") == 0 else None
    if hou.isUIAvailable():
        hou.ui.displayMessage("Deleted %d Google-derived area(s)." % len(gone))


def on_export_usd(kwargs):
    node = kwargs["node"]
    path = node.parm("usd_path").evalAsString() or default_usd_path(node)
    n = export_usd(node, path)
    hou.ui.displayMessage("Wrote %s (%d meshes)" % (path, n)) if hou.isUIAvailable() else None


# ------------------------------------------------------------------ USD
def default_usd_path(node):
    from . import paths
    return os.path.join(paths.export_dir(), hou.text.variableName(node.evalParm("area_name") or "area") + ".usdc")


def export_usd(node, path):
    """Write the cooked output as a Y-up, metres USD stage with UsdPreviewSurface
    materials (one Mesh per chunk x material) plus geospatial metadata."""
    from pxr import Usd, UsdGeom, UsdShade, Sdf, Gf, Vt
    import numpy as np

    geo = node.node("strip_temp").geometry()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    stage = Usd.Stage.CreateNew(path) if not os.path.exists(path) else Usd.Stage.Open(path)
    stage.GetRootLayer().Clear()
    UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.y)
    UsdGeom.SetStageMetersPerUnit(stage, 1.0)
    name = hou.text.variableName(node.parm("area_name").evalAsString() or "Twinscape")
    root = UsdGeom.Xform.Define(stage, "/" + name)
    stage.SetDefaultPrim(root.GetPrim())
    rp = root.GetPrim()
    rp.SetCustomDataByKey("earthtwin:lat", node.evalParm("lat"))
    rp.SetCustomDataByKey("earthtwin:lon", node.evalParm("lon"))
    rp.SetCustomDataByKey("earthtwin:convention", "x=East y=Up z=South metres")
    man_p = os.path.join(node.parm("area_dir").evalAsString(), "manifest.json")
    if os.path.exists(man_p):
        with open(man_p) as f:
            rp.SetCustomDataByKey("earthtwin:attribution", " / ".join(json.load(f).get("attribution", [])))

    # materials
    mats = {}
    looks = UsdGeom.Scope.Define(stage, "/%s/Looks" % name)

    def material(shop):
        if shop in mats:
            return mats[shop]
        mnode = hou.node(shop)
        mname = hou.text.variableName(mnode.name() if mnode else "default")
        mat = UsdShade.Material.Define(stage, "%s/%s" % (looks.GetPath(), mname))
        sh = UsdShade.Shader.Define(stage, mat.GetPath().AppendChild("Surface"))
        sh.CreateIdAttr("UsdPreviewSurface")
        rough = mnode.evalParm("rough") if mnode and mnode.parm("rough") else 0.8
        sh.CreateInput("roughness", Sdf.ValueTypeNames.Float).Set(float(rough))
        sh.CreateInput("metallic", Sdf.ValueTypeNames.Float).Set(0.0)
        tex = mnode.parm("basecolor_texture").evalAsString() if mnode and mnode.parm("basecolor_texture") else ""
        if tex:
            st = UsdShade.Shader.Define(stage, mat.GetPath().AppendChild("st"))
            st.CreateIdAttr("UsdPrimvarReader_float2")
            st.CreateInput("varname", Sdf.ValueTypeNames.Token).Set("st")
            tx = UsdShade.Shader.Define(stage, mat.GetPath().AppendChild("diffuse"))
            tx.CreateIdAttr("UsdUVTexture")
            tx.CreateInput("file", Sdf.ValueTypeNames.Asset).Set(tex)
            tx.CreateInput("st", Sdf.ValueTypeNames.Float2).ConnectToSource(st.ConnectableAPI(), "result")
            tx.CreateInput("wrapS", Sdf.ValueTypeNames.Token).Set("repeat")
            tx.CreateInput("wrapT", Sdf.ValueTypeNames.Token).Set("repeat")
            tx.CreateInput("sourceColorSpace", Sdf.ValueTypeNames.Token).Set("sRGB")
            tx.CreateOutput("rgb", Sdf.ValueTypeNames.Float3)
            sh.CreateInput("diffuseColor", Sdf.ValueTypeNames.Color3f).ConnectToSource(tx.ConnectableAPI(), "rgb")
        else:
            sh.CreateInput("diffuseColor", Sdf.ValueTypeNames.Color3f).Set(Gf.Vec3f(0.5, 0.5, 0.5))
        mat.CreateSurfaceOutput().ConnectToSource(sh.ConnectableAPI(), "surface")
        mats[shop] = mat
        return mat

    P = np.frombuffer(geo.pointFloatAttribValuesAsString("P"), np.float32).reshape(-1, 3)
    chunk_attr = geo.findPrimAttrib("chunk")
    shop_attr = geo.findPrimAttrib("shop_materialpath")
    groups = {}
    for prim in geo.iterPrims():
        key = (prim.attribValue(chunk_attr) if chunk_attr else "all",
               prim.attribValue(shop_attr) if shop_attr else "")
        groups.setdefault(key, []).append(prim)
    uv_attr = geo.findVertexAttrib("uv")
    n_attr = geo.findVertexAttrib("N")
    count = 0
    for (chunk, shop), prims in sorted(groups.items()):
        counts, idx, sts, nrm = [], [], [], []
        for pr in prims:
            vs = pr.vertices()
            counts.append(len(vs))
            for v in reversed(vs):  # Houdini CW -> USD CCW (rightHanded)
                idx.append(v.point().number())
                if uv_attr:
                    u = v.attribValue(uv_attr)
                    sts.append((u[0], u[1]))
                if n_attr:
                    nrm.append(tuple(v.attribValue(n_attr)))
        used = sorted(set(idx))
        remap = {o: i for i, o in enumerate(used)}
        mname = hou.text.variableName(os.path.basename(shop) or "mesh")
        mesh = UsdGeom.Mesh.Define(stage, "/%s/Geo/%s_%s" % (name, hou.text.variableName(chunk), mname))
        mesh.CreatePointsAttr(Vt.Vec3fArray.FromNumpy(P[used]))
        mesh.CreateFaceVertexCountsAttr(counts)
        mesh.CreateFaceVertexIndicesAttr([remap[i] for i in idx])
        mesh.CreateSubdivisionSchemeAttr("none")
        if sts:
            pv = UsdGeom.PrimvarsAPI(mesh).CreatePrimvar("st", Sdf.ValueTypeNames.TexCoord2fArray,
                                                         UsdGeom.Tokens.faceVarying)
            pv.Set(sts)
        if nrm:
            mesh.CreateNormalsAttr(nrm)
            mesh.SetNormalsInterpolation(UsdGeom.Tokens.faceVarying)
        if shop:
            UsdShade.MaterialBindingAPI.Apply(mesh.GetPrim()).Bind(material(shop))
        count += 1
    stage.GetRootLayer().Save()
    return count
