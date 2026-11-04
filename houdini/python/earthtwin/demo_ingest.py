"""Twinscape ingestion demo (Houdini side), recorded with macOS screen capture.

The real mouse cursor is moved with CoreGraphics and every click is a real widget action (map
recentre, combo selection, typing, Fetch & Build), with a small ripple so clicks read on video.
After the build it shows the mesh in the viewport, its Unreal chunks, the HDA network and the
attributes Houdini Engine hands to Unreal in the Geometry Spreadsheet.

    from earthtwin import demo_ingest as di
    di.run(record_prefix="~/Twinscape/renders/houdini")      # or record_prefix=None to rehearse
"""
import ctypes
import math
import os
import time

import hou
from PySide6 import QtCore, QtGui, QtWidgets

from . import demo_director as dd
from . import ui

TARGET = dict(lat=40.7484, lon=-73.9857, name="Midtown 3D Tiles", size=1000.0)
NODE = "twinscape_midtown_3d_tiles"
LOG = []


def _log(s):
    LOG.append("%7.2f %s" % (time.time() - STATE["t0"], s))


STATE = {"t0": time.time(), "events": []}

# ------------------------------------------------------------------ real cursor
_cg = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")


class _P(ctypes.Structure):
    _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]


_cg.CGWarpMouseCursorPosition.argtypes = [_P]
_cg.CGAssociateMouseAndMouseCursorPosition.argtypes = [ctypes.c_int]


def warp(x, y):
    _cg.CGWarpMouseCursorPosition(_P(float(x), float(y)))
    _cg.CGAssociateMouseAndMouseCursorPosition(1)


class Ripple(QtWidgets.QWidget):
    """Expanding ring at a global point so a click is visible in the recording."""
    def __init__(self, gp):
        super().__init__(None, QtCore.Qt.Tool | QtCore.Qt.FramelessWindowHint |
                         QtCore.Qt.WindowStaysOnTopHint | QtCore.Qt.WindowTransparentForInput)
        self.setAttribute(QtCore.Qt.WA_TranslucentBackground)
        self.setAttribute(QtCore.Qt.WA_ShowWithoutActivating)
        self.setAttribute(QtCore.Qt.WA_DeleteOnClose)
        r = 34
        self.setGeometry(int(gp.x() - r), int(gp.y() - r), 2 * r, 2 * r)
        self.t = 0.0
        self.anim = QtCore.QVariantAnimation(self, startValue=0.0, endValue=1.0, duration=420)
        self.anim.valueChanged.connect(self._tick)
        self.anim.finished.connect(self.close)
        self.show()
        self.anim.start()

    def _tick(self, v):
        self.t = v
        self.update()

    def paintEvent(self, ev):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        a = int(230 * (1 - self.t))
        rad = 8 + 24 * self.t
        p.setPen(QtGui.QPen(QtGui.QColor(255, 196, 92, a), 3))
        p.setBrush(QtGui.QColor(255, 196, 92, int(a * 0.25)))
        c = QtCore.QPointF(self.width() / 2.0, self.height() / 2.0)
        p.drawEllipse(c, rad, rad)


_KEEP = []


def ripple(gp):
    _KEEP.append(Ripple(gp))
    del _KEEP[:-8]
    STATE["events"].append((time.time() - STATE["t0"], "click", gp.x(), gp.y()))


# ------------------------------------------------------------------ step builders
def _ease(u):
    return u * u * (3 - 2 * u)


def move(target, ms=650, fps=60):
    """Glide the cursor from wherever it is to target() (global QPoint, evaluated at run time)."""
    holder = {}
    n = max(2, int(ms / 1000.0 * fps))
    steps = []

    def start():
        holder["a"] = QtGui.QCursor.pos()
        holder["b"] = target()
    steps.append(("do", start, 1))
    for i in range(1, n + 1):
        def f(i=i):
            u = _ease(i / float(n))
            a, b = holder["a"], holder["b"]
            warp(a.x() + (b.x() - a.x()) * u, a.y() + (b.y() - a.y()) * u)
        steps.append(("do", f, 1000.0 / fps))
    return steps


def gpos(widget_fn, fx=0.5, fy=0.5, dx=0, dy=0):
    """Global point inside a widget (fractions of its size)."""
    def f():
        w = widget_fn()
        return w.mapToGlobal(QtCore.QPoint(int(w.width() * fx) + dx, int(w.height() * fy) + dy))
    return f


def click(action=None, pause=350):
    def f():
        ripple(QtGui.QCursor.pos())
        if action:
            action()
    return [("do", f, pause)]


def type_text(edit_fn, text, cps=16, select_all=True):
    steps = []
    if select_all:
        steps.append(("do", lambda: (edit_fn().setFocus(), edit_fn().selectAll()), 250))
    for i in range(1, len(text) + 1):
        steps.append(("do", lambda i=i: edit_fn().setText(text[:i]), 1000.0 / cps))
    return steps


def wait(ms):
    return [("do", lambda: None, ms)]


# ------------------------------------------------------------------ scene prep
def _viewer():
    return hou.ui.paneTabOfType(hou.paneTabType.SceneViewer)


def _net():
    return hou.ui.paneTabOfType(hou.paneTabType.NetworkEditor)


def _details():
    return [p for p in hou.ui.curDesktop().paneTabs() if p.type() == hou.paneTabType.DetailsView][0]


def _maximize(tab):
    """Give one pane the whole Houdini window (more pixels for the recording)."""
    for p in hou.ui.curDesktop().panes():
        if p.isMaximized() and p != tab.pane():
            p.setIsMaximized(False)
    tab.pane().setIsMaximized(True)


def unmaximize_all():
    for p in hou.ui.curDesktop().panes():
        if p.isMaximized():
            p.setIsMaximized(False)


def prepare():
    """Hide every object, drop any previous demo node, park a camera for the orbit."""
    obj = hou.node("/obj")
    old = obj.node(NODE)
    if old:
        old.destroy()
    for c in obj.children():
        c.setDisplayFlag(False)
    cam = obj.node("et_demo_cam") or obj.createNode("cam", "et_demo_cam")
    for k, v in dict(resx=1920, resy=1080, focal=28, far=50000).items():
        cam.parm(k).set(v)
    cam.setDisplayFlag(False)
    v = _viewer()
    v.setIsCurrentTab()
    vp = v.curViewport()
    vp.setCamera(cam)
    _cam_pose(cam, 200, 1500, 900, 0)
    net = _net()
    net.setPwd(obj)
    return cam


def _cam_pose(cam, ang, radius, height, target_h, centre=(0.0, 0.0)):
    a = math.radians(ang)
    p = (centre[0] + radius * math.sin(a), height, centre[1] + radius * math.cos(a))
    t = (centre[0], target_h, centre[1])
    dx, dy, dz = t[0] - p[0], t[1] - p[1], t[2] - p[2]
    cam.parmTuple("t").set(p)
    cam.parmTuple("r").set((math.degrees(math.atan2(dy, math.hypot(dx, dz))), math.degrees(math.atan2(-dx, -dz)), 0))


def orbit(cam, a0, a1, seconds, radius, height, target_h=120.0, r1=None, h1=None, fps=30):
    n = int(seconds * fps)
    out = []
    for i in range(n + 1):
        u = _ease(i / float(n))
        r = radius + ((r1 if r1 is not None else radius) - radius) * u
        h = height + ((h1 if h1 is not None else height) - height) * u
        out.append(("do", lambda a=a0 + (a1 - a0) * u, r=r, h=h: _cam_pose(cam, a, r, h, target_h), 1000.0 / fps))
    return out


def _chunk_view(geo_node):
    """unpack -> colour every prim by its Unreal chunk (one colour = one Nanite static mesh)."""
    up = geo_node.node("chunks_unpack") or geo_node.createNode("unpack", "chunks_unpack")
    up.setInput(0, geo_node.node("city_builder"))
    w = geo_node.node("chunk_colours") or geo_node.createNode("attribwrangle", "chunk_colours")
    w.setInput(0, up)
    w.parm("class").set(1)
    w.parm("snippet").set(
        'string c = prim(0, "chunk", @primnum);\n'
        'float h = rand(random_shash(c));\n'
        '@Cd = hsvtorgb(set(h, 0.55, 0.95));\n'
        's@shop_materialpath = "";\n')
    w.moveToGoodPosition()
    up.moveToGoodPosition()
    return w


# ------------------------------------------------------------------ the show
def run(record_prefix=None, chunk=20, fetch_force=False, parts=("A", "B"), stop_before_fetch=False):
    """Part A: panel -> pick region -> sources -> Fetch & Build (live download).
    Part B: the built twin in Houdini (viewport, Unreal chunks, network, attributes)."""
    STATE.update(t0=time.time(), events=[], done=False)
    del LOG[:]
    holder = {}
    if "A" in parts:
        cam = prepare()
    else:
        cam = hou.node("/obj/et_demo_cam")
        holder["node"] = hou.node("/obj/%s/city_builder" % NODE)
        holder["geo"] = holder["node"].parent()
        _cam_pose(cam, 200, 1500, 900, 0)
    P = lambda: holder["p"]

    def open_panel():
        mw = hou.qt.mainWindow()
        scr = QtWidgets.QApplication.primaryScreen().availableGeometry()
        mw.showMaximized()
        p = holder["p"] = ui.show(None)
        p.resize(1230, 930)
        p.move(scr.x() + scr.width() - p.width() - 24, scr.y() + 40)
        p.name.setText("Area")
        p.mode.setCurrentIndex(0)
        p._on_mode()
        p.g3d_fine.setValue(3.0)
        p.map.zoom = 12
        p.lat.setValue(40.7300)
        p.lon.setValue(-73.9950)
        p.size.setValue(TARGET["size"])
        p._sync_map()
        warp(scr.x() + scr.width() * 0.25, scr.y() + scr.height() * 0.55)
        _log("panel open")

    def map_target():
        m = P().map
        return m.mapToGlobal(m.ll_to_px(TARGET["lat"], TARGET["lon"]).toPoint())

    def map_click_target():
        m = P().map
        m.click_at(m.ll_to_px(TARGET["lat"], TARGET["lon"]))

    def zoom_in():
        m = P().map
        m.zoom += 1
        m.update()

    def paste_go():
        P()._on_paste()

    def pick_mode():
        p = P()
        p.mode.showPopup()

    def choose_g3d():
        p = P()
        i = p.mode.findData("google3d")
        view = p.mode.view()
        view.setCurrentIndex(p.mode.model().index(i, 0))

    def close_mode():
        p = P()
        p.mode.hidePopup()
        p.mode.setCurrentIndex(p.mode.findData("google3d"))
        p._on_mode()
        p._update_estimate()

    def step_down():
        p = P()
        p.g3d_fine.stepDown()

    def fetch():
        p = P()
        _log("fetch & build")
        if fetch_force:
            p._fetch(True)
        else:
            p.go.click()

    def fetch_done():
        p = P()
        return p._thread is None and ("Done" in p.status.text() or "Failed" in p.status.text())

    def after_build():
        p = P()
        _log("built: " + p.status.text())
        holder["node"] = p.node
        holder["geo"] = p.node.parent()
        p.node.parent().setDisplayFlag(True)

    def slide_panel_away():
        p = P()
        p.hide()                      # (window slides are clamped on macOS)
        return
        g = p.geometry()
        a = QtCore.QPropertyAnimation(p, b"pos", p)
        a.setDuration(700)
        a.setStartValue(g.topLeft())
        a.setEndValue(QtCore.QPoint(g.x() + g.width() + 60, g.y()))
        a.setEasingCurve(QtCore.QEasingCurve.InOutCubic)
        a.finished.connect(p.hide)
        holder["anim"] = a
        a.start()

    def show_viewport():
        v = _viewer()
        v.setIsCurrentTab()
        _maximize(v)
        v.curViewport().setCamera(cam)
        v.setCurrentNode(holder["node"])
        _net().setPwd(holder["geo"])
        holder["node"].setSelected(True, clear_all_selected=True)
        try:
            _net().homeToSelection()
        except Exception:
            pass

    def chunks_on():
        g = holder["geo"]
        w = _chunk_view(g)
        w.setDisplayFlag(True)
        w.setRenderFlag(True)
        _net().setPwd(g)
        w.setSelected(True, clear_all_selected=True)
        _log("chunks on")

    def chunks_off():
        n = holder["node"]
        n.setDisplayFlag(True)
        n.setRenderFlag(True)
        n.setSelected(True, clear_all_selected=True)
        _log("chunks off")

    def dive():
        n = holder["node"]
        net = _net()
        _maximize(net)
        net.setPwd(n)
        key = ("load_area", "g3d_clip_x0", "g3d_N", "unreal_assign", "unreal_detail", "pack_chunks", "OUT")
        for c in n.children():
            c.setSelected(c.name() in key or c.name().startswith("g3d_clip"), clear_all_selected=False)
        try:
            net.frameSelection()
        except Exception:
            pass
        _log("network")

    def sheet(attrib_type, filt, node_name="strip_temp"):
        def f():
            d = _details()
            d.setIsCurrentTab()
            _maximize(d)
            n = holder["node"].node(node_name) or holder["node"].node("OUT")
            d.setCurrentNode(n)
            d.setAttribType(attrib_type)
            if filt:
                d.setAttribFilter(filt)
                d.setAttribFilterEnabled(True)
            else:
                d.setAttribFilterEnabled(False)
            _log("sheet %s" % attrib_type)
        return f

    def back_to_viewer():
        _net().setPwd(holder["geo"])
        holder["node"].setSelected(True, clear_all_selected=True)
        _viewer().setIsCurrentTab()
        _maximize(_viewer())

    def stop_rec():
        if record_prefix:
            dd.stop_recording()

    S = [("do", lambda: dd.activate(dd.HOUDINI_APP), 1600)]
    if "A" in parts:
        S += [("do", open_panel, 2200)]
    else:
        S += [("do", lambda: (ui._PANEL and ui._PANEL.hide(), hou.qt.mainWindow().showMaximized(), show_viewport()), 1500)]
    if record_prefix:
        S += [("do", lambda: dd.record(os.path.expanduser(record_prefix), chunk=chunk), 1800)]
    if "A" in parts:
        # 1. find the place on the map
        S += move(map_target, 900) + click(map_click_target, 700)
        for _ in range(3):
            S += [("do", zoom_in, 750)]
        S += move(map_target, 350) + click(map_click_target, 900)
        # 2. exact coordinates by paste, a name, the size
        S += move(gpos(lambda: P().paste), 700) + click(lambda: P().paste.setFocus(), 250)
        S += type_text(lambda: P().paste, "40.7484, -73.9857", select_all=False) + wait(300)
        S += [("do", paste_go, 900)]
        S += move(gpos(lambda: P().name), 600) + click(None, 150) + type_text(lambda: P().name, TARGET["name"]) + wait(500)
        S += move(gpos(lambda: P().size), 500) + wait(700)
        # 3. source: Google Photorealistic 3D Tiles
        S += move(gpos(lambda: P().mode), 700) + click(pick_mode, 1300)
        S += [("do", choose_g3d, 900)] + click(close_mode, 1600)
        S += move(gpos(lambda: P().key), 600) + wait(1000)
        # 4. street-detail level of detail
        S += move(gpos(lambda: P().g3d_err), 500) + wait(500)
        S += move(gpos(lambda: P().g3d_fine, fx=1.0, fy=0.75, dx=-9), 600)
        S += click(step_down, 450) + click(step_down, 900)
        S += move(gpos(lambda: P().estimate, fx=0.3), 500) + wait(1800)
        S += move(gpos(lambda: P().nanite), 500) + wait(700)
        # 5. fetch & build
        S += move(gpos(lambda: P().go), 700)
        if not stop_before_fetch:
            S += click(fetch, 800)
            S += move(gpos(lambda: P().log, fx=0.5, fy=0.3), 900)
            S += [("wait", fetch_done, 300), ("do", after_build, 1500)]
            S += move(gpos(lambda: P().log, fx=0.6, fy=0.9), 600) + wait(1500)
            S += [("do", slide_panel_away, 900), ("do", show_viewport, 600)]
    if "B" in parts:
        S += [("do", lambda: warp(1150, 995), 10)]
        # 6. the twin in Houdini
        S += orbit(cam, 200, 255, 9.0, 1500, 900, 0, r1=900, h1=520)
        S += [("do", chunks_on, 300)] + orbit(cam, 255, 290, 5.0, 900, 520, 0) + [("do", chunks_off, 300)]
        S += orbit(cam, 290, 320, 4.0, 900, 520, 0, r1=520, h1=330)
        S += [("do", dive, 4200)]
        S += [("do", sheet(hou.attribType.Prim, "chunk unreal_* shop_materialpath material_override g3d_page g3d_tile et_kind"), 5200)]
        S += [("do", sheet(hou.attribType.Point, "P uv"), 3000)]
        S += [("do", sheet(hou.attribType.Global, "", "unreal_detail"), 5200)]
        S += [("do", back_to_viewer, 600)]
        S += orbit(cam, 320, 360, 6.0, 520, 330, 60, r1=760, h1=600)
    S += [("do", stop_rec, 100), ("do", unmaximize_all, 100), ("do", lambda: STATE.update(done=True), 10)]
    tl = dd.Timeline(S)
    dd._TL["tl"] = tl
    tl.start()
    return len(S)
