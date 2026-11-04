"""Builds the earthtwin::city_builder::1.0 SOP HDA from scratch.

    from earthtwin import build_hda; build_hda.build()
"""
import os

import hou

from . import providers

HDA_NAME = "earthtwin::city_builder::1.0"
LABEL = "Twinscape City Builder"

# ---------------------------------------------------------------- VEX
VEX_COMMON = r'''
float et_height(string dir; int n; float size; vector P) {
    float half = size * 0.5;
    float fu = clamp((P.x + half) / size, 0, 1);
    float fv = clamp((P.z + half) / size, 0, 1);       // 0 = north edge
    float s = (fu * (n - 1) + 0.5) / n;
    float t = 1.0 - (fv * (n - 1) + 0.5) / n;
    vector h = texture(dir + "/height.exr", s, t, "filter", "point", "wrap", "clamp", "srccolorspace", "linear");
    return h.x;
}
vector et_ortho_uv(float size; vector P) {
    float half = size * 0.5;
    return set((P.x + half) / size, 1.0 - (P.z + half) / size, 0);
}
'''

VEX_TERRAIN = VEX_COMMON + r'''
string dir = detail(1, "area_dir", 0);
int n = int(detail(1, "dem_n"));
float size = float(detail(1, "size"));
float eref = float(detail(1, "elev_ref"));
float h = et_height(dir, n, size, @P);
f@elev = h + eref;
@P.y = h * ch("../height_scale");
if (chi("../water") && f@elev < ch("../water_level") + ch("../shore_band")) {
    @P.y -= ch("../water_carve");
    i@underwater = 1;
}
v@uv = et_ortho_uv(size, @P);
'''

VEX_BLD_PREP = VEX_COMMON + r'''
string dir = detail(0, "area_dir", 0);
int n = int(detail(0, "dem_n"));
float size = float(detail(0, "size"));
float hs = ch("../height_scale");
float area = primintrinsic(0, "measuredarea", @primnum);
if (area < ch("../bld_min_area")) { removeprim(0, @primnum, 1); return; }
int pts[] = primpoints(0, @primnum);
float gmin = 1e9, gmax = -1e9;
vector c = 0;
foreach (int pt; pts) {
    vector p = point(0, "P", pt);
    float g = et_height(dir, n, size, p) * hs;
    gmin = min(gmin, g); gmax = max(gmax, g);
    c += p;
}
c /= max(1, len(pts));
float bs = ch("../bld_height_scale");
float mh = max(0.0, f@min_height) * bs;
float h = f@height;
if (h <= 1.0) {
    h = ch("../bld_default_height") * fit01(rand(i@bld_id * 0.731 + 3.1), 0.6, 1.5);
    if (mh > 0) h = mh / bs + 3.0;
}
h *= bs;
float gmid = 0.5 * (gmin + gmax);
f@height = h;
f@base = (mh > 0.1) ? gmid + mh : gmin - ch("../bld_embed");
f@top = max(gmid + h, f@base + 1.0);
v@ctr = set(c.x, 0, c.z);
foreach (int pt; pts) {
    vector p = point(0, "P", pt);
    setpointattrib(0, "P", pt, set(p.x, f@base, p.z));
}
// ---- roof: minimum width (rotating calipers over polygon edges)
float wmin = 1e9;
for (int i = 0; i < len(pts); i++) {
    vector a = point(0, "P", pts[i]);
    vector b = point(0, "P", pts[(i + 1) % len(pts)]);
    vector e = set(b.x - a.x, 0, b.z - a.z);
    if (length(e) < 1e-4) continue;
    vector nrm = normalize(set(-e.z, 0, e.x));
    float lo = 1e9, hi = -1e9;
    foreach (int pt; pts) { vector pp = point(0, "P", pt); float t = dot(pp, nrm); lo = min(lo, t); hi = max(hi, t); }
    wmin = min(wmin, hi - lo);
}
f@wmin = wmin;
string rs = s@roof_shape;
int pitched = chi("../roofs") && rs != "" && rs != "flat";
float rh = f@roof_height * bs;
if (pitched && rh <= 0.05) {
    if (rs == "dome" || rs == "onion")        rh = 0.5 * wmin;
    else if (rs == "pyramidal" || rs == "cone") rh = 0.35 * wmin;
    else if (rs == "skillion")                rh = 0.15 * wmin;
    else if (rs == "mansard")                 rh = min(4.0, 0.25 * wmin);
    else                                      rh = 0.3 * wmin * ch("../roof_pitch_scale");
}
if (pitched) rh = clamp(rh, 0.3, 0.8 * (f@top - f@base));
i@pitched = pitched;
f@roof_h = pitched ? rh : 0.0;
f@eave = f@top - f@roof_h;
float r = rand(i@bld_id * 1.37 + 5.0);
int st;
if (h > 150)      st = (r < 0.85) ? 2 : 3;
else if (h > 80)  st = (r < 0.6) ? 2 : 3;
else if (h > 30)  st = (r < 0.35) ? 2 : ((r < 0.7) ? 3 : 0);
else              st = (r < 0.6) ? 1 : 0;
i@style = st;
'''

VEX_ROOF_SNAP = r'''
// extruded front points -> eave (pitched) or top (flat)
int prims[] = pointprims(0, @ptnum);
float top = -1e9;
int isroof = 0;
foreach (int pr; prims) {
    if (inprimgroup(0, "roof", pr)) isroof = 1;
    top = max(top, prim(0, "eave", pr));
}
if (isroof) @P.y = top;
'''

VEX_CUT = r'''
// pitched buildings get generated roofs; floating parts keep their underside
if (i@pitched && inprimgroup(0, "roof", @primnum)) removeprim(0, @primnum, 0);
if (inprimgroup(0, "bottom", @primnum) && f@min_height < 0.1) removeprim(0, @primnum, 0);
'''

VEX_ROOF_GEN = r'''
// prim wrangle over upward-facing footprints: builds pitched roofs, removes every input prim.
void tag(int pr; int src; string grp) {
    int g = geoself();
    foreach (string a; split("bld_id style")) { int v = prim(0, a, src); setprimattrib(g, a, pr, v); }
    foreach (string a; split("base top eave roof_h min_height height")) { float v = prim(0, a, src); setprimattrib(g, a, pr, v); }
    int one = 1;
    setprimattrib(g, "pitched", pr, one);
    string rs = prim(0, "roof_shape", src);
    setprimattrib(g, "roof_shape", pr, rs);
    vector ctr = prim(0, "ctr", src);
    setprimattrib(g, "ctr", pr, ctr);
    vector4 bc = prim(0, "bcol", src);
    setprimattrib(g, "bcol", pr, bc);
    setprimgroup(g, grp, pr, 1);
}
int addp(vector p) { return addpoint(geoself(), p); }
int poly3(int a; int b; int c) { return addprim(geoself(), "poly", a, b, c); }
int poly4(int a; int b; int c; int d) { return addprim(geoself(), "poly", a, b, c, d); }

int src = @primnum;
if (!i@pitched) { removeprim(0, src, 1); return; }
string rs = s@roof_shape;
float eave = f@eave;
float rh = f@roof_h;
vector up = {0, 1, 0};
int ip[] = primpoints(0, src);
int n = len(ip);
vector Q[];
vector c = 0;
foreach (int pt; ip) { vector p = point(0, "P", pt); p.y = eave; append(Q, p); c += p; }
c /= n;
int g = geoself();

if (rs == "pyramidal" || rs == "cone") {
    int apex = addp(c + up * rh);
    int ring[]; foreach (vector q; Q) append(ring, addp(q));
    for (int i = 0; i < n; i++) tag(poly3(ring[i], ring[(i + 1) % n], apex), src, "roof");
}
else if (rs == "dome" || rs == "onion") {
    int K = 12;
    int prev[]; foreach (vector q; Q) append(prev, addp(q));
    for (int k = 1; k <= K; k++) {
        float t = float(k) / K;
        if (k == K) {
            int apex = addp(c + up * rh);
            for (int i = 0; i < n; i++) tag(poly3(prev[i], prev[(i + 1) % n], apex), src, "roof");
            break;
        }
        float r, y;
        if (rs == "dome") { float th = t * PI * 0.5; r = cos(th); y = sin(th); }
        else { r = (1.0 - t) * (1.0 + 0.9 * sin(PI * min(1.0, t * 1.3))); y = t; }
        int ring[];
        foreach (vector q; Q) append(ring, addp(c + (q - c) * r + up * (rh * y)));
        for (int i = 0; i < n; i++) tag(poly4(prev[i], prev[(i + 1) % n], ring[(i + 1) % n], ring[i]), src, "roof");
        prev = ring;
    }
}
else if (rs == "skillion") {
    vector D;
    if (f@roof_dir >= 0) { float a = radians(f@roof_dir); D = set(sin(a), 0, -cos(a)); }
    else {
        float best = -1; vector e = {1, 0, 0};
        for (int i = 0; i < n; i++) { vector d = Q[(i + 1) % n] - Q[i]; if (length(d) > best) { best = length(d); e = d; } }
        D = normalize(cross(up, e));
    }
    float tmin = 1e9, tmax = -1e9;
    foreach (vector q; Q) { float t = dot(q, D); tmin = min(tmin, t); tmax = max(tmax, t); }
    float span = max(1e-3, tmax - tmin);
    int low[], high[];
    foreach (vector q; Q) {
        append(low, addp(q));
        append(high, addp(q + up * (rh * (tmax - dot(q, D)) / span)));
    }
    tag(addprim(g, "poly", high), src, "roof");
    for (int i = 0; i < n; i++) {
        int j = (i + 1) % n;
        tag(poly4(low[i], low[j], high[j], high[i]), src, "walls");
    }
}
else if (rs == "mansard") {
    float inset = min(0.18 * f@wmin, 3.0);
    int low[], hi[];
    foreach (vector q; Q) {
        vector d = c - q; float L = length(d);
        float k = (L > 1e-3) ? min(inset / L, 0.45) : 0;
        append(low, addp(q));
        append(hi, addp(q + d * k + up * rh));
    }
    for (int i = 0; i < n; i++) { int j = (i + 1) % n; tag(poly4(low[i], low[j], hi[j], hi[i]), src, "roof"); }
    tag(addprim(g, "poly", hi), src, "roof");
}
else if (n == 4) {
    float L0 = distance(Q[0], Q[1]), L1 = distance(Q[1], Q[2]);
    int along0 = (L0 >= L1);
    if (i@roof_across) along0 = !along0;
    vector a, b, cc, d;
    if (along0) { a = Q[0]; b = Q[1]; cc = Q[2]; d = Q[3]; }
    else        { a = Q[1]; b = Q[2]; cc = Q[3]; d = Q[0]; }
    if (rs == "hipped" || rs == "half-hipped") {
        vector dir = normalize(b - a);
        float L = distance(a, b), W = distance(a, d);
        float inset = min((rs == "hipped") ? W * 0.5 : W * 0.25, L * 0.5);
        int pa = addp(a), pb = addp(b), pc = addp(cc), pd = addp(d);
        int m0 = addp((a + d) * 0.5 + dir * inset + up * rh);
        int m1 = addp((b + cc) * 0.5 - dir * inset + up * rh);
        tag(poly4(pa, pb, m1, m0), src, "roof");
        tag(poly4(pc, pd, m0, m1), src, "roof");
        tag(poly3(pb, pc, m1), src, "roof");
        tag(poly3(pd, pa, m0), src, "roof");
    } else {
        float U[], H[];
        if (rs == "gambrel")      { U = array(0.0, 0.18, 0.5, 0.82, 1.0); H = array(0.0, 0.65, 1.0, 0.65, 0.0); }
        else if (rs == "saltbox") { U = array(0.0, 0.35, 1.0); H = array(0.0, 1.0, 0.0); }
        else if (rs == "round")   { for (int k = 0; k <= 8; k++) { float u = k / 8.0; append(U, u); append(H, sqrt(max(0.0, 1.0 - pow(2.0 * u - 1.0, 2.0)))); } }
        else                      { U = array(0.0, 0.5, 1.0); H = array(0.0, 1.0, 0.0); }
        int A[], B[];
        for (int k = 0; k < len(U); k++) {
            append(A, addp(lerp(a, d, U[k]) + up * (rh * H[k])));
            append(B, addp(lerp(b, cc, U[k]) + up * (rh * H[k])));
        }
        for (int k = 0; k < len(U) - 1; k++) tag(poly4(A[k], B[k], B[k + 1], A[k + 1]), src, "roof");
        tag(addprim(g, "poly", A), src, "walls");   // gable ends
        tag(addprim(g, "poly", B), src, "walls");
    }
}
else {
    // non-rectangular pitched footprints: remeshed + straight-skeleton-like height field downstream
    int ring[]; foreach (vector q; Q) append(ring, addp(q));
    int pr = addprim(g, "poly", ring);
    tag(pr, src, "rfield");
}
removeprim(0, src, 1);
'''

VEX_ROOF_FIELD = r'''
// detail wrangle: height = profile(distance to footprint edge / max distance) - a straight-skeleton
// height field, exact for convex hipped roofs. input1 = footprints (bld_id).
float dist2seg(vector p; vector a; vector b) {
    vector ab = b - a; float t = clamp(dot(p - a, ab) / max(1e-9, dot(ab, ab)), 0, 1);
    return distance(p, a + ab * t);
}
int np = npoints(0);
float D[]; resize(D, np);
int F[]; resize(F, np);
float maxd[]; resize(maxd, nprimitives(1));
for (int pt = 0; pt < np; pt++) {
    int prs[] = pointprims(0, pt);
    if (len(prs) == 0) continue;
    int bid = prim(0, "bld_id", prs[0]);
    int fp = findattribval(1, "prim", "bld_id", bid);
    F[pt] = fp;
    if (fp < 0) continue;
    int fpts[] = primpoints(1, fp);
    vector p = point(0, "P", pt); p.y = 0;
    float d = 1e9;
    for (int i = 0; i < len(fpts); i++) {
        vector a = point(1, "P", fpts[i]); a.y = 0;
        vector b = point(1, "P", fpts[(i + 1) % len(fpts)]); b.y = 0;
        d = min(d, dist2seg(p, a, b));
    }
    D[pt] = d;
    maxd[fp] = max(maxd[fp], d);
}
for (int pt = 0; pt < np; pt++) {
    int prs[] = pointprims(0, pt);
    if (len(prs) == 0 || F[pt] < 0) continue;
    float m = maxd[F[pt]];
    float u = (m > 1e-4) ? D[pt] / m : 0;
    string rs = prim(0, "roof_shape", prs[0]);
    float h = u;
    if (rs == "gambrel" || rs == "round") h = sin(u * PI * 0.5);
    float eave = prim(0, "eave", prs[0]);
    float rh = prim(0, "roof_h", prs[0]);
    vector p = point(0, "P", pt);
    setpointattrib(0, "P", pt, set(p.x, eave + rh * h, p.z));
}
'''

VEX_ORIENT = r'''
// flag faces that point the wrong way: roofs up, gable / side walls outward
vector N = prim_normal(0, @primnum, 0.5, 0.5);
vector pc = primuv(0, "P", @primnum, set(0.5, 0.5, 0));
if (inprimgroup(0, "walls", @primnum)) {
    vector out = set(pc.x - v@ctr.x, 0, pc.z - v@ctr.z);
    i@flip = (dot(set(N.x, 0, N.z), out) < 0);
} else {
    i@flip = (N.y < 0);
}
'''

VEX_BLD_UV = r'''
// vertex wrangle: facade UVs on walls, ortho on roofs
float size = float(detail(0, "size"));
if (inprimgroup(0, "walls", @primnum)) {
    vector N = prim_normal(0, @primnum, 0.5, 0.5);
    vector d = normalize(cross(set(0, 1, 0), N));
    float tw = ch("../facade_tile_w");
    float th = ch("../facade_tile_h");
    float off = floor(rand(i@bld_id) * 8.0) * 0.25;
    v@uv = set(dot(v@P, d) / tw + off, (v@P.y - f@base) / th, 0);
} else {
    float half = size * 0.5;
    v@uv = set((v@P.x + half) / size, 1.0 - (v@P.z + half) / size, 0);
}
'''

VEX_COLOUR = r'''
// OSM building:colour -> vertex colour on walls. White = untinted (the Unreal facade material only
// recolours the wall, never the glass). Alpha stays 1: Houdini's viewport treats Alpha as opacity.
vector4 bc = p@bcol;
vector col = {1, 1, 1};
if (chi("../use_colour") && bc.w > 0.5 && inprimgroup(0, "walls", @primnum))
    col = lerp(col, min(set(bc.x, bc.y, bc.z), {0.985, 0.985, 0.985}), ch("../colour_strength"));
v@Cd = col;
'''

VEX_ASSIGN = r'''
// prim wrangle: materials, chunks, Unreal output attributes
string root = chs("../unreal_mat_root");
string aid  = detail(0, "area_id", 0);
string slug = detail(0, "area_slug", 0);
float size  = float(detail(0, "size"));
float cs    = max(1.0, ch("../chunk_size"));
float half  = size * 0.5;
string kind = s@et_kind;
vector c = (kind == "bld") ? v@ctr : primuv(0, "P", @primnum, set(0.5, 0.5, 0));
int cx = int(floor(clamp(c.x + half, 0, size - 1e-3) / cs));
int cz = int(floor(clamp(c.z + half, 0, size - 1e-3) / cs));
string pre = (kind == "bld") ? "Bld" : ((kind == "water") ? "Water" : ((kind == "g3d") ? "G3D" : "Terrain"));
s@chunk = (kind == "water") ? "Water" : sprintf("%s_%02d_%02d", pre, cx, cz);
s@unreal_bake_name = sprintf("SM_%s_%s", slug, s@chunk);
string ortho = sprintf("%s/Areas/%s/MI_Ortho.MI_Ortho", root, aid);
if (kind == "bld" && inprimgroup(0, "walls", @primnum)) {
    s@shop_materialpath = sprintf("%s/facade_%d", chs("../matnet_path"), i@style);
    s@unreal_material = sprintf("%s/Shared/MI_Facade_%d.MI_Facade_%d", root, i@style, i@style);
} else if (kind == "water") {
    s@shop_materialpath = chs("../matnet_path") + "/water";
    s@unreal_material = sprintf("%s/Shared/MI_Water.MI_Water", root);
} else if (kind == "g3d") {
    int pg = i@g3d_page;
    s@shop_materialpath = chs("../matnet_path") + "/g3d";
    string adir = detail(0, "area_dir", 0);
    s@material_override = sprintf("{'basecolor_texture':'%s/g3d_atlas_%d.jpg'}", adir, pg);
    s@unreal_material = sprintf("%s/Areas/%s/MI_G3D_%d.MI_G3D_%d", root, aid, pg, pg);
} else {
    s@shop_materialpath = chs("../matnet_path") + "/ortho";
    s@unreal_material = ortho;
}
'''

VEX_COPY_DETAIL = r'''
foreach (string a; split("area_dir area_id area_slug cache_dir attribution")) { string v = detail(1, a, 0); setdetailattrib(0, a, v); }
foreach (string a; split("size elev_ref elev_min lat lon")) { float v = detail(1, a); setdetailattrib(0, a, v); }
foreach (string a; split("dem_n g3d_pages")) { int v = int(detail(1, a)); setdetailattrib(0, a, v); }
'''

VEX_DETAIL = r'''
string root = chs("../unreal_bake_root");
s@unreal_split_attr = "chunk";
s@unreal_bake_folder = sprintf("%s/Areas/%s/Meshes", root, s@area_id);
i@unreal_nanite_enabled = chi("../nanite");
s@earthtwin_version = "1.1";
'''


def _wr(parent, name, code, cls="point", inputs=(), group=""):
    n = parent.createNode("attribwrangle", name)
    n.parm("class").set({"detail": 0, "primitive": 1, "point": 2, "vertex": 3}[cls])
    n.parm("snippet").set(code.strip() + "\n")
    if group:
        n.parm("group").set(group)
    for i, src in enumerate(inputs):
        if src is not None:
            n.setInput(i, src)
    return n


def _menu(items):
    return [k for k, _ in items], [v for _, v in items]


def _principled(matnet, name, tex, rough, spec=0.5, color=(0.5, 0.5, 0.5)):
    m = matnet.createNode("principledshader::2.0", name)
    m.parm("rough").set(rough)
    m.parm("reflect").set(spec)
    m.parmTuple("basecolor").set(color)
    if tex:
        m.parm("basecolor_useTexture").set(1)
        m.parm("basecolor_texture").set(tex)
        m.parmTuple("basecolor").set((1, 1, 1))
    return m


def build_network(sub):
    for c in sub.children():
        c.destroy()
    # ------------------------------------------------ data
    load = sub.createNode("python", "load_area")
    load.parm("python").set("node = hou.pwd()\nnode.parent().hdaModule().cook_load(node)\n")

    # ------------------------------------------------ terrain
    grid = sub.createNode("grid", "terrain_grid")
    grid.parm("orient").set("zx")
    grid.parm("sizex").setExpression('detail("../load_area", "size", 0)')
    grid.parm("sizey").setExpression('detail("../load_area", "size", 0)')
    grid.parm("rows").setExpression('detail("../load_area", "dem_n", 0)')
    grid.parm("cols").setExpression('detail("../load_area", "dem_n", 0)')
    th = _wr(sub, "terrain_height", VEX_TERRAIN, "point", (grid, load))
    tred = sub.createNode("polyreduce::2.0", "terrain_reduce")
    tred.setInput(0, th)
    tred.parm("percentage").setExpression('ch("../terrain_keep")')
    tred.parm("boundaryweight").set(3.0)
    tsw = sub.createNode("switch", "terrain_reduce_sw")
    tsw.setInput(0, th)
    tsw.setInput(1, tred)
    tsw.parm("input").setExpression('ch("../terrain_keep") < 99.9')
    tn = sub.createNode("normal", "terrain_N")
    tn.setInput(0, tsw)
    tn.parm("type").set(0)  # point normals: smooth ground
    tpr = sub.createNode("attribpromote", "terrain_uv_to_vtx")
    tpr.setInput(0, tn)
    tpr.parm("inname").set("uv")
    tpr.parm("inclass").set(2)   # point
    tpr.parm("outclass").set(3)  # vertex
    tk = _wr(sub, "terrain_kind", 's@et_kind = "terrain";', "primitive", (tpr,))

    # ------------------------------------------------ water
    water = _wr(sub, "water_plane", r'''
float size = float(detail(1, "size"));
float half = size * 0.5;
float emin = detail(1, "elev_min");
float eref = detail(1, "elev_ref");
if (!chi("../water") || emin > ch("../water_level") + ch("../shore_band")) return;
float y = (ch("../water_level") - eref) * ch("../height_scale");
int p0 = addpoint(0, set(-half, y,  half));
int p1 = addpoint(0, set( half, y,  half));
int p2 = addpoint(0, set( half, y, -half));
int p3 = addpoint(0, set(-half, y, -half));
int pr = addprim(0, "poly", p0, p3, p2, p1);
setprimattrib(0, "et_kind", pr, "water");
''', "detail", (None, load))
    wn = sub.createNode("normal", "water_N")
    wn.setInput(0, water)
    wuv = _wr(sub, "water_uv", 'v@uv = set(v@P.x / 50.0, v@P.z / 50.0, 0);', "vertex", (wn,))

    # ------------------------------------------------ buildings
    clean = sub.createNode("clean", "bld_clean")
    clean.setInput(0, load)
    prep = _wr(sub, "bld_prep", VEX_BLD_PREP, "primitive", (clean,))
    pn = sub.createNode("normal", "bld_footN")
    pn.setInput(0, prep)
    pn.parm("type").set(2)  # primitive
    rev = sub.createNode("reverse", "bld_face_up")
    rev.setInput(0, pn)
    rev.parm("group").set("@N.y<0")
    ext = sub.createNode("polyextrude::2.0", "bld_extrude")
    ext.setInput(0, rev)
    ext.parm("dist").set(1.0)
    ext.parm("outputfront").set(1)
    ext.parm("outputside").set(1)
    ext.parm("outputfrontgrp").set(1)
    ext.parm("frontgrp").set("roof")
    ext.parm("outputsidegrp").set(1)
    ext.parm("sidegrp").set("walls")
    ext.parm("outputback").set(1)
    ext.parm("outputbackgrp").set(1)
    ext.parm("backgrp").set("bottom")
    snap = _wr(sub, "bld_roof_snap", VEX_ROOF_SNAP, "point", (ext,))
    cut = _wr(sub, "bld_cut_roofs", VEX_CUT, "primitive", (snap,))

    # ---- pitched roofs (OSM roof:shape)
    rgen = _wr(sub, "roof_gen", VEX_ROOF_GEN, "primitive", (rev,))
    rf_only = sub.createNode("blast", "roof_field_only")
    rf_only.setInput(0, rgen)
    rf_only.parm("group").set("rfield")
    rf_only.parm("negate").set(1)
    rf_rest = sub.createNode("blast", "roof_exact")
    rf_rest.setInput(0, rgen)
    rf_rest.parm("group").set("rfield")
    rmesh = sub.createNode("remesh::2.0", "roof_remesh")
    rmesh.setInput(0, rf_only)
    rmesh.parm("targetsize").setExpression('ch("../roof_remesh_size")')
    rmesh.parm("iterations").set(2)
    rfield = _wr(sub, "roof_field", VEX_ROOF_FIELD, "detail", (rmesh, rev))
    rgrp = sub.createNode("groupcreate", "roof_field_grp")
    rgrp.setInput(0, rfield)
    rgrp.parm("groupname").set("roof")
    rmerge = sub.createNode("merge", "roofs_merge")
    rmerge.setInput(0, rf_rest)
    rmerge.setInput(1, rgrp)
    rori = _wr(sub, "roof_orient", VEX_ORIENT, "primitive", (rmerge,))
    rrev = sub.createNode("reverse", "roof_fix_winding")
    rrev.setInput(0, rori)
    rrev.parm("group").set("@flip==1")
    rclean = sub.createNode("attribdelete", "roof_strip")
    rclean.setInput(0, rrev)
    rclean.parm("doprimdel").set(1)
    rclean.parm("primdel").set("flip N")
    rclean.parm("doptdel").set(1)
    rclean.parm("ptdel").set("N")
    bmerge = sub.createNode("merge", "bld_with_roofs")
    bmerge.setInput(0, cut)
    bmerge.setInput(1, rclean)
    bdel = sub.createNode("attribdelete", "bld_cleanN")
    bdel.setInput(0, bmerge)
    bdel.parm("doprimdel").set(1)
    bdel.parm("primdel").set("N")
    bN = sub.createNode("normal", "bld_N")
    bN.setInput(0, bdel)
    bN.parm("type").set(1)  # vertex
    bN.parm("cuspangle").set(30)
    buv = _wr(sub, "bld_uv", VEX_BLD_UV, "vertex", (bN,))
    bcolr = _wr(sub, "bld_colour", VEX_COLOUR, "primitive", (buv,))
    bk = _wr(sub, "bld_kind", 's@et_kind = "bld";', "primitive", (bcolr,))
    bsw = sub.createNode("switch", "bld_enable")
    bsw.setInput(0, sub.createNode("null", "no_buildings"))
    bsw.setInput(1, bk)
    bsw.parm("input").setExpression('ch("../buildings")')

    # ------------------------------------------------ google 3d tiles
    # clip the tile set to the capture square (tiles overhang the area)
    prev = load
    for nm, d in (("east", (-1, 0, 0)), ("west", (1, 0, 0)), ("south", (0, 0, -1)), ("north", (0, 0, 1))):
        cl = sub.createNode("clip::2.0", "g3d_clip_" + nm)
        cl.setInput(0, prev)
        toks = cl.parm("clipop").parmTemplate().menuItems()
        cl.parm("clipop").set([t for t in toks if "above" in t.lower()][0])
        cl.parmTuple("dir").set(d)
        sgn = '-' if (d[0] + d[2]) > 0 else ''
        axis = "x" if d[0] else "z"
        cl.parm("origin" + axis).setExpression('%sdetail("../load_area", "size", 0) * 0.5' % sgn)
        prev = cl
    gN = sub.createNode("normal", "g3d_N")
    gN.setInput(0, prev)
    gN.parm("type").set(1)
    gN.parm("cuspangle").set(45)
    gk = _wr(sub, "g3d_kind", 's@et_kind = "g3d";', "primitive", (gN,))

    # ------------------------------------------------ assemble
    mproc = sub.createNode("merge", "merge_open_data")
    mproc.setInput(0, tk)
    mproc.setInput(1, bsw)
    mproc.setInput(2, wuv)
    mg3d = sub.createNode("merge", "merge_google")
    mg3d.setInput(0, gk)
    mode = sub.createNode("switch", "mode_switch")
    mode.setInput(0, mproc)
    mode.setInput(1, mg3d)
    mode.parm("input").setExpression('ch("../mode")')
    # carry detail attribs (lost on terrain branch) back from load_area
    cpd = _wr(sub, "copy_area_detail", VEX_COPY_DETAIL, "detail", (mode, load))
    assign = _wr(sub, "unreal_assign", VEX_ASSIGN, "primitive", (cpd,))
    det = _wr(sub, "unreal_detail", VEX_DETAIL, "detail", (assign,))
    # robust triangulation of concave footprints/roofs/gable ends: Houdini Engine and USD
    # consumers fan-triangulate n-gons, which produces shards on concave polygons
    tri = sub.createNode("divide", "triangulate_buildings")
    tri.setInput(0, det)
    tri.parm("group").set("@et_kind=bld")
    clean_attr = sub.createNode("attribdelete", "strip_temp")
    clean_attr.setInput(0, tri)
    clean_attr.parm("doprimdel").set(1)
    clean_attr.parm("primdel").set("ctr base top et_kind eave roof_h wmin pitched bcol rcol flip")
    clean_attr.parm("doptdel").set(1)
    clean_attr.parm("ptdel").set("underwater")
    # one packed primitive per chunk -> Houdini Engine emits one static mesh per chunk
    pack = sub.createNode("pack", "pack_chunks")
    pack.setInput(0, clean_attr)
    pack.parm("packbyname").set(1)
    pack.parm("nameattribute").set("chunk")
    pack.parm("packedfragments").set(0)
    pack.parm("pivot").set(0)
    pack.parm("transfer_attributes").set("chunk unreal_bake_name")
    outer = _wr(sub, "unreal_detail_outer", VEX_DETAIL + 'i@unreal_force_instancer = 0;\n', "detail", (pack, load))
    outer.parm("snippet").set('s@area_id = detail(1, "area_id", 0);\n' + outer.parm("snippet").eval())
    osw = sub.createNode("switch", "split_switch")
    osw.setInput(0, clean_attr)
    osw.setInput(1, outer)
    osw.parm("input").setExpression('ch("../split_chunks")')
    out = sub.createNode("null", "OUT")
    out.setInput(0, osw)
    out.setDisplayFlag(True)
    out.setRenderFlag(True)
    out.setColor(hou.Color(0.2, 0.6, 1.0))
    load.setColor(hou.Color(1.0, 0.8, 0.2))
    out_mat = sub.createNode("output", "output0")
    out_mat.setInput(0, out)

    # ------------------------------------------------ materials (Houdini viewport / Karma)
    matnet = sub.createNode("matnet", "matnet")
    ad = '`details("../../load_area", "area_dir")`'
    cd = '`details("../../load_area", "cache_dir")`'
    _principled(matnet, "ortho", ad + "/ortho.jpg", 0.85, 0.35)
    for k in range(len(providers.FACADE_STYLES)):
        _principled(matnet, "facade_%d" % k, cd + "/_shared/facade_%d.jpg" % k, 0.55 if k != 2 else 0.2, 0.5)
    _principled(matnet, "water", "", 0.05, 0.6, (0.05, 0.12, 0.14))
    # Google 3D Tiles are authored unlit (KHR_materials_unlit). Houdini's GL viewport can't show
    # emission textures, so preview with the base colour texture, no specular; Unreal gets the
    # unlit M_ET_Photo through unreal_material.
    _principled(matnet, "g3d", ad + "/g3d_atlas_0.jpg", 1.0, 0.0)
    matnet.layoutChildren()
    sub.layoutChildren()
    return out


# ---------------------------------------------------------------- interface
def parm_templates():
    T = hou
    g = hou.ParmTemplateGroup()

    loc = T.FolderParmTemplate("f_loc", "Location", folder_type=hou.folderType.Tabs)
    loc.addParmTemplate(T.ButtonParmTemplate("open_panel", "Open Twinscape Panel",
                        script_callback="hou.phm().on_open_panel(kwargs)",
                        script_callback_language=hou.scriptLanguage.Python,
                        help="Map-based area picker, sources, progress and export."))
    pre = T.StringParmTemplate("preset", "Preset", 1, default_value=("",),
                               menu_type=hou.menuType.Normal,
                               script_callback="hou.phm().on_preset(kwargs)",
                               script_callback_language=hou.scriptLanguage.Python)
    pre.setItemGeneratorScript("hou.phm().preset_menu()")
    pre.setItemGeneratorScriptLanguage(hou.scriptLanguage.Python)
    loc.addParmTemplate(pre)
    loc.addParmTemplate(T.StringParmTemplate("area_name", "Area Name", 1, default_value=("Lower Manhattan",)))
    loc.addParmTemplate(T.FloatParmTemplate("lat", "Latitude", 1, default_value=(40.7080,), min=-85, max=85,
                                            help="WGS84 latitude of the area centre (local origin)."))
    loc.addParmTemplate(T.FloatParmTemplate("lon", "Longitude", 1, default_value=(-74.0115,), min=-180, max=180))
    loc.addParmTemplate(T.FloatParmTemplate("size", "Size (m)", 1, default_value=(1200,), min=100, max=5000,
                                            min_is_strict=True, help="Side of the square capture area in metres."))
    g.append(loc)

    src = T.FolderParmTemplate("f_src", "Sources", folder_type=hou.folderType.Tabs)
    src.addParmTemplate(T.MenuParmTemplate("mode", "Mode", ("procedural", "google3d"),
                        ("Open Data (terrain + imagery + extruded buildings)",
                         "Google Photorealistic 3D Tiles (API key, look-dev)"), default_value=0))
    for name, label, items, d in (("terrain_src", "Terrain", providers.TERRAIN_SOURCES, 0),
                                  ("imagery_src", "Imagery", providers.IMAGERY_SOURCES, 0),
                                  ("buildings_src", "Buildings", providers.BUILDING_SOURCES, 0)):
        k, v = _menu(items)
        src.addParmTemplate(T.MenuParmTemplate(name, label, k, v, default_value=d))
    key = T.StringParmTemplate("api_key", "Google API Key", 1, default_value=("",),
                               help="Leave empty to use $GOOGLE_MAPS_API_KEY or ~/.twinscape/google_api_key.")
    key.setTags({"editor": "0"})
    src.addParmTemplate(key)
    src.addParmTemplate(T.FloatParmTemplate("dem_res", "Terrain Spacing (m)", 1, default_value=(2.0,), min=0.5, max=30))
    src.addParmTemplate(T.FloatParmTemplate("ortho_res", "Imagery Resolution (m/px)", 1, default_value=(0.3,), min=0.1, max=10))
    src.addParmTemplate(T.MenuParmTemplate("max_tex", "Max Texture Size", ("2048", "4096", "8192", "16384"),
                                           ("2K", "4K", "8K", "16K"), default_value=2))
    src.addParmTemplate(T.FloatParmTemplate("g3d_error", "3D Tiles Error (m)", 1, default_value=(6.0,), min=0.5, max=64,
                                            help="Geometric error threshold for Google 3D Tiles traversal. Lower = finer. "
                                                 "With Street Detail on, this is the error at the edges of the area."))
    src.addParmTemplate(T.FloatParmTemplate("g3d_focus_error", "Street Detail Error (m)", 1, default_value=(2.5,), min=0.0, max=16,
                                            help="View-dependent LOD: tiles near the area centre refine to this error. In Manhattan "
                                                 "Google's finest tiles are ~2 m, so 2.5 reaches full street-level geometry and "
                                                 "texture near the centre. 0 = uniform error."))
    src.addParmTemplate(T.FloatParmTemplate("g3d_focus_radius", "Street Detail Radius (m)", 1, default_value=(150.0,), min=10, max=2000,
                                            help="Full street detail within this distance of the centre; beyond it the allowed "
                                                 "error grows with distance up to 3D Tiles Error."))
    src.addParmTemplate(T.FloatParmTemplate("g3d_hole", "Context Ring Hole (m)", 1, default_value=(0.0,), min=0.0, max=10000,
                                            help="Build a coarse context ring around an existing detailed area: tiles and "
                                                 "triangles inside a centred square of this size are skipped and the ground "
                                                 "datum is taken from the cached detailed area of that size. 0 = off."))
    g.append(src)

    data = T.FolderParmTemplate("f_data", "Data", folder_type=hou.folderType.Tabs)
    data.addParmTemplate(T.ButtonParmTemplate("fetch", "Fetch",
                         script_callback="hou.phm().on_fetch(kwargs)",
                         script_callback_language=hou.scriptLanguage.Python,
                         help="Download and project all layers for the current settings (Shift-click to force re-download)."))
    data.addParmTemplate(T.ButtonParmTemplate("open_folder", "Open Area Folder",
                         script_callback="hou.phm().on_open_folder(kwargs)",
                         script_callback_language=hou.scriptLanguage.Python))
    data.addParmTemplate(T.ButtonParmTemplate("purge_google", "Delete Google Data",
                         script_callback="hou.phm().on_purge_google(kwargs)",
                         script_callback_language=hou.scriptLanguage.Python,
                         help="Remove every cached area built from Google content."))
    data.addParmTemplate(T.ToggleParmTemplate("auto_fetch", "Auto Fetch On Cook", default_value=True,
                         help="Fetch automatically when the area isn't cached (needed for Houdini Engine)."))
    data.addParmTemplate(T.StringParmTemplate("cache_dir", "Cache Folder", 1,
                         default_value=("",),
                         string_type=hou.stringParmType.FileReference, file_type=hou.fileType.Directory,
                         help="Where areas are downloaded and cached. Leave empty for <repo>/cache "
                              "($TWINSCAPE_CACHE overrides)."))
    ad = T.StringParmTemplate("area_dir", "Area Folder", 1, default_value=("",),
                              default_expression=("hou.phm().area_dir_expr(hou.pwd())",),
                              default_expression_language=(hou.scriptLanguage.Python,))
    ad.setDisableWhen("{ auto_fetch != 2 }")
    data.addParmTemplate(ad)
    info = T.StringParmTemplate("info", "Info", 1, default_value=("",),
                                default_expression=("hou.phm().info_text(hou.pwd())",),
                                default_expression_language=(hou.scriptLanguage.Python,))
    info.setTags({"editor": "1", "editorlines": "4-6"})
    info.setDisableWhen("{ auto_fetch != 2 }")
    data.addParmTemplate(info)
    g.append(data)

    ter = T.FolderParmTemplate("f_ter", "Terrain", folder_type=hou.folderType.Tabs)
    ter.addParmTemplate(T.FloatParmTemplate("height_scale", "Height Scale", 1, default_value=(1.0,), min=0, max=5))
    ter.addParmTemplate(T.FloatParmTemplate("terrain_keep", "Keep Triangles %", 1, default_value=(100.0,), min=1, max=100,
                        help="PolyReduce the terrain (100 = full grid; Nanite handles full density)."))
    ter.addParmTemplate(T.ToggleParmTemplate("water", "Water Plane", default_value=True))
    ter.addParmTemplate(T.FloatParmTemplate("water_level", "Water Level (m, absolute)", 1, default_value=(0.3,),
                        min=-20, max=50, disable_when="{ water == 0 }"))
    ter.addParmTemplate(T.FloatParmTemplate("shore_band", "Shore Band (m)", 1, default_value=(0.6,), min=0, max=5,
                        disable_when="{ water == 0 }"))
    ter.addParmTemplate(T.FloatParmTemplate("water_carve", "Carve Depth (m)", 1, default_value=(2.0,), min=0, max=20,
                        disable_when="{ water == 0 }"))
    g.append(ter)

    bld = T.FolderParmTemplate("f_bld", "Buildings", folder_type=hou.folderType.Tabs)
    bld.addParmTemplate(T.ToggleParmTemplate("buildings", "Buildings", default_value=True))
    bld.addParmTemplate(T.FloatParmTemplate("bld_height_scale", "Height Scale", 1, default_value=(1.0,), min=0.1, max=5))
    bld.addParmTemplate(T.FloatParmTemplate("bld_default_height", "Default Height (m)", 1, default_value=(10.0,), min=2, max=100,
                        help="Used when the source has no height (randomised 0.6-1.5x)."))
    bld.addParmTemplate(T.FloatParmTemplate("bld_min_area", "Min Footprint (m2)", 1, default_value=(6.0,), min=0, max=200))
    bld.addParmTemplate(T.FloatParmTemplate("bld_embed", "Embed Depth (m)", 1, default_value=(0.5,), min=0, max=5))
    bld.addParmTemplate(T.ToggleParmTemplate("roofs", "Roof Shapes (OSM roof:shape)", default_value=True,
                        help="Gabled, hipped, pyramidal, skillion, dome, onion, mansard, gambrel, round."))
    bld.addParmTemplate(T.FloatParmTemplate("roof_pitch_scale", "Default Pitch Scale", 1, default_value=(1.0,),
                        min=0.2, max=3, disable_when="{ roofs == 0 }",
                        help="Scales the roof height used when OSM gives a shape but no roof:height."))
    bld.addParmTemplate(T.FloatParmTemplate("roof_remesh_size", "Irregular Roof Detail (m)", 1, default_value=(0.75,),
                        min=0.2, max=5, disable_when="{ roofs == 0 }",
                        help="Remesh size for non-rectangular pitched roofs (height-field method)."))
    bld.addParmTemplate(T.ToggleParmTemplate("use_colour", "Use OSM building:colour", default_value=True))
    bld.addParmTemplate(T.FloatParmTemplate("colour_strength", "Colour Strength", 1, default_value=(0.75,), min=0, max=1,
                        disable_when="{ use_colour == 0 }"))
    bld.addParmTemplate(T.FloatParmTemplate("facade_tile_w", "Facade Tile Width (m)", 1, default_value=(14.0,), min=1, max=60))
    bld.addParmTemplate(T.FloatParmTemplate("facade_tile_h", "Facade Tile Height (m)", 1, default_value=(14.4,), min=1, max=60))
    g.append(bld)

    ue = T.FolderParmTemplate("f_ue", "Unreal", folder_type=hou.folderType.Tabs)
    ue.addParmTemplate(T.FloatParmTemplate("chunk_size", "Chunk Size (m)", 1, default_value=(300.0,), min=50, max=2000,
                       help="Meshes are split into chunks for culling, streaming and HLOD."))
    ue.addParmTemplate(T.ToggleParmTemplate("split_chunks", "Split Into Chunk Meshes", default_value=True,
                       help="Pack per chunk so Houdini Engine creates one static mesh per chunk (culling, streaming, HLOD)."))
    ue.addParmTemplate(T.ToggleParmTemplate("nanite", "Nanite", default_value=True))
    ue.addParmTemplate(T.ToggleParmTemplate("collision", "Complex As Simple Collision", default_value=True))
    ue.addParmTemplate(T.StringParmTemplate("unreal_mat_root", "Material Root", 1, default_value=("/Game/Twinscape",)))
    ue.addParmTemplate(T.StringParmTemplate("unreal_bake_root", "Bake Root", 1, default_value=("/Game/Twinscape",)))
    mp = T.StringParmTemplate("matnet_path", "Houdini Matnet", 1, default_value=("",),
                              default_expression=("hou.pwd().node('matnet').path()",),
                              default_expression_language=(hou.scriptLanguage.Python,))
    mp.hide(True)
    ue.addParmTemplate(mp)
    ue.addParmTemplate(T.StringParmTemplate("usd_path", "USD File", 1, default_value=("",),
                       string_type=hou.stringParmType.FileReference,
                       help="Leave empty for <repo>/export/<Area Name>.usdc"))
    ue.addParmTemplate(T.ButtonParmTemplate("export_usd", "Export USD",
                       script_callback="hou.phm().on_export_usd(kwargs)",
                       script_callback_language=hou.scriptLanguage.Python))
    g.append(ue)
    return g


PYTHON_MODULE = r'''
import os, sys, importlib
def _ensure_path():
    try:
        lib = hou.nodeType(hou.sopNodeTypeCategory(), "earthtwin::city_builder::1.0").definition().libraryFilePath()
    except Exception:
        lib = ""
    cands = [os.path.normpath(os.path.join(os.path.dirname(lib), "..", "python")) if lib else "",
             os.environ.get("TWINSCAPE", "") and os.path.join(os.environ["TWINSCAPE"], "houdini", "python"),
             os.environ.get("EARTHTWIN", "") and os.path.join(os.environ["EARTHTWIN"], "python"),
             os.path.join(os.path.expanduser("~"), "EarthTwin", "python")]
    for c in cands:
        if c and os.path.isdir(os.path.join(c, "earthtwin")):
            if c not in sys.path:
                sys.path.insert(0, c)
            return c
_ensure_path()
from earthtwin import hda as _et

def cook_load(sop): return _et.cook_load(sop)
def area_dir_expr(node): return _et.area_dir_expr(node)
def info_text(node): return _et.info_text(node)
def preset_menu(): return _et.preset_menu()
def on_preset(kw): _et.on_preset(kw["node"])
def on_fetch(kw): _et.on_fetch(kw)
def on_open_panel(kw): _et.on_open_panel(kw)
def on_open_folder(kw): _et.on_open_folder(kw)
def on_export_usd(kw): _et.on_export_usd(kw)
def on_purge_google(kw): _et.on_purge_google(kw)
def default_cache_dir():
    from earthtwin import paths
    return paths.cache_dir()
def default_usd_path(node):
    from earthtwin import paths
    return os.path.join(paths.export_dir(), hou.text.variableName(node.evalParm("area_name") or "area") + ".usdc")
def reload_all():
    import earthtwin
    for m in ("paths", "geo", "net", "providers", "ingest", "google3d", "hda", "ui"):
        importlib.reload(importlib.import_module("earthtwin." + m))
'''

HELP = r'''= Twinscape City Builder =

#icon: SOP/terrain

"""Builds real-world terrain, orthoimagery and buildings for Unreal (Houdini Engine or USD)."""

== Workflow ==
# Pick a preset or enter lat/lon/size (or use the panel's map).
# Choose sources. Open data: USGS 3DEP terrain, USGS NAIP imagery, NYC Open Data / OSM buildings.
# Press Fetch. Data is cached per area; the cook never needs the network once cached.
# In Unreal: drop the HDA, cook, run `Twinscape > Prepare Area` (or the pipeline script), bake.

== Output attributes ==
:chunk: prim string, used with `unreal_split_attr` to split into one static mesh per chunk.
:unreal_material: prim string, Unreal material instance per prim.
:unreal_bake_name / unreal_bake_folder: names and folder of baked assets.
:unreal_nanite_enabled: detail int.

== Licensing ==
Google Map Tiles content is subject to Google's terms (no caching/offline/derived geometry).
USGS data is public domain; OSM is ODbL; NYC Open Data requires attribution.
'''


def build(save_dir=None):
    from . import paths
    save_dir = save_dir or paths.otls_dir()
    os.makedirs(save_dir, exist_ok=True)
    path = os.path.join(save_dir, "earthtwin_city_builder.hda")
    # temp container
    obj = hou.node("/obj")
    tmp = obj.node("_earthtwin_build") or obj.createNode("geo", "_earthtwin_build")
    for c in tmp.children():
        c.destroy()
    sub = tmp.createNode("subnet", "city_builder")
    # drop the default subnet output connector so we control inside
    defn_existing = hou.nodeType(hou.sopNodeTypeCategory(), HDA_NAME)
    # snapshot every instance's user-set values: replacing the definition resets them
    saved = {}
    if defn_existing:
        for inst in defn_existing.instances():
            vals = {}
            for parm in inst.parms():
                try:
                    parm.expression()
                    continue            # keep expression-driven parms (area_dir, info, ...)
                except hou.OperationFailed:
                    pass
                if not parm.isAtDefault():
                    vals[parm.name()] = parm.evalAsString() if parm.parmTemplate().type() in (
                        hou.parmTemplateType.String, hou.parmTemplateType.Menu) else parm.eval()
            saved[inst.path()] = vals
    if defn_existing:
        for d in defn_existing.allInstalledDefinitions():
            if d.libraryFilePath() == path:
                hou.hda.uninstallFile(path)
                break
    asset = sub.createDigitalAsset(name=HDA_NAME, hda_file_name=path, description=LABEL,
                                   min_num_inputs=0, max_num_inputs=0, ignore_external_references=True,
                                   change_node_type=True, create_backup=False)
    defn = asset.type().definition()
    defn.setParmTemplateGroup(parm_templates())
    defn.addSection("PythonModule", PYTHON_MODULE)
    defn.setExtraFileOption("PythonModule/IsPython", True)
    defn.addSection("Help", HELP)
    defn.setIcon("SOP_terrain")
    asset.allowEditingOfContents()
    build_network(asset)
    defn.updateFromNode(asset)
    defn.setVersion("1.1")
    defn.save(path, asset)
    asset.matchCurrentDefinition()
    for pth, vals in saved.items():
        inst = hou.node(pth)
        if inst is None:
            continue
        for k, v in vals.items():
            parm = inst.parm(k)
            if parm is not None:
                try:
                    parm.set(v)
                except Exception:
                    pass
    return asset, path
