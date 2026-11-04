"""Houdini-side demo director: drives the Twinscape panel and viewport on a timeline (QTimer),
so a screen recording shows the real tool working, and starts/stops macOS screen capture.

    from earthtwin import demo_director as dd
    dd.record("/path/clip.mov", seconds=150)
    dd.run_houdini_segment("Midtown (Empire State)")
"""
import math
import os
import subprocess
import time

import hou
from PySide6 import QtCore, QtWidgets

from . import ui

STATE = {"step": "", "done": False, "rec": None, "t0": 0.0, "log": []}
# macOS only (screen recording helpers). Override with $TWINSCAPE_HOUDINI_APP / $TWINSCAPE_UNREAL_APP.
import glob as _glob
HOUDINI_APP = os.environ.get("TWINSCAPE_HOUDINI_APP") or next(
    iter(sorted(_glob.glob("/Applications/Houdini/Houdini*/Houdini*.app"), reverse=True)), "")
UNREAL_APP = os.environ.get("TWINSCAPE_UNREAL_APP") or next(
    iter(sorted(_glob.glob("/Users/Shared/Epic Games/UE_*/Engine/Binaries/Mac/UnrealEditor.app"), reverse=True)), "")


def log(s):
    STATE["log"].append("%6.1f %s" % (time.time() - STATE["t0"], s))
    STATE["step"] = s


def activate(app):
    subprocess.Popen(["open", "-a", app])


def record(path_prefix, seconds=None, chunk=20):
    """Continuous capture as back-to-back fixed-length clips (<prefix>_000.mov ...).
    `screencapture -V n` only writes the movie if it runs to completion, so stopping is done
    by letting the current chunk finish. Gaps between chunks are ~0.3 s."""
    import threading
    path_prefix = path_prefix.rsplit(".mov", 1)[0]
    STATE["rec_on"] = True
    STATE["rec_files"] = []

    def loop():
        i = 0
        t0 = time.time()
        while STATE["rec_on"] and (seconds is None or time.time() - t0 < seconds):
            f = "%s_%03d.mov" % (path_prefix, i)
            if os.path.exists(f):
                os.remove(f)
            STATE["rec"] = subprocess.Popen(["screencapture", "-v", "-C", "-V", str(int(chunk)), "-x", f],
                                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            STATE["rec"].wait()
            STATE["rec_files"].append(f)
            i += 1
        STATE["rec_on"] = False
    threading.Thread(target=loop, daemon=True).start()
    return path_prefix


def recording():
    return bool(STATE.get("rec_on")) or (STATE.get("rec") is not None and STATE["rec"].poll() is None)


def stop_recording():
    """Request stop; the running chunk completes (up to `chunk` seconds) and is kept."""
    STATE["rec_on"] = False


# --------------------------------------------------------------- timeline engine
class Timeline(QtCore.QObject):
    def __init__(self, steps):
        super().__init__()
        self.steps = list(steps)
        self.timer = QtCore.QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self._next)

    def start(self):
        STATE["done"] = False
        STATE["t0"] = time.time()
        self._next()

    def _next(self):
        if not self.steps:
            STATE["done"] = True
            log("timeline done")
            return
        kind, a, b = self.steps.pop(0)
        try:
            if kind == "do":            # ("do", fn, delay_after_ms)
                a()
                self.timer.start(int(b))
            elif kind == "wait":        # ("wait", predicate, poll_ms)
                if a():
                    self.timer.start(10)
                else:
                    self.steps.insert(0, (kind, a, b))
                    self.timer.start(int(b))
        except Exception as ex:          # keep the show going, note the problem
            log("ERROR %s" % ex)
            self.timer.start(200)


_TL = {"tl": None}


def _cam_orbit(cam, centre, radius, height, a0, a1, seconds, fps=30, target_h=120.0):
    frames = int(seconds * fps)

    def bind():
        v = _viewer()
        if v and v.curViewport().camera() != cam:
            v.curViewport().setCamera(cam)
    steps = [("do", bind, 50)]
    for i in range(frames + 1):
        u = i / float(frames)
        u = u * u * (3 - 2 * u)
        a = math.radians(a0 + (a1 - a0) * u)
        p = (centre[0] + radius * math.sin(a), height, centre[2] + radius * math.cos(a))
        t = (centre[0], target_h, centre[2])

        def f(p=p, t=t):
            dx, dy, dz = t[0] - p[0], t[1] - p[1], t[2] - p[2]
            cam.parmTuple("t").set(p)
            cam.parmTuple("r").set((math.degrees(math.atan2(dy, math.hypot(dx, dz))),
                                    math.degrees(math.atan2(-dx, -dz)), 0))
        steps.append(("do", f, 1000.0 / fps))
    return steps


def _viewer():
    return hou.ui.paneTabOfType(hou.paneTabType.SceneViewer)


def _combo_peek(combo, ms=1300):
    """Open a combo's dropdown briefly so the recording shows the choices."""
    return [("do", combo.showPopup, ms), ("do", combo.hidePopup, 300)]


def run_houdini_segment(preset="Midtown (Empire State)", node_name="earthtwin_midtown", mode="procedural",
                        area_name=None, highlight=("roof_gen", "bld_prep", "unreal_assign", "pack_chunks")):
    obj = hou.node("/obj")
    old = obj.node(node_name)
    if old:
        old.destroy()
    for c in obj.children():
        c.setDisplayFlag(False)
    cam = obj.node("et_demo_cam") or obj.createNode("cam", "et_demo_cam")
    cam.parm("resx").set(1920)
    cam.parm("resy").set(1080)
    cam.parm("focal").set(28)
    cam.parm("far").set(50000)
    cam.setDisplayFlag(False)
    holder = {}

    def open_panel():
        mw = hou.qt.mainWindow()
        scr = QtWidgets.QApplication.primaryScreen().availableGeometry()
        mw.setGeometry(scr)
        holder["p"] = p = ui.show(None)
        p.name.setText("")
        p.resize(1180, 900)
        scr = QtWidgets.QApplication.primaryScreen().availableGeometry()
        p.move(scr.x() + scr.width() - p.width() - 30, scr.y() + 60)
        p.map.zoom = 13
        p.map.set_area(40.7128, -74.0060, 1000.0)
        log("panel open")

    def pick_preset():
        p = holder["p"]
        i = p.preset.findText(preset)
        p.preset.setCurrentIndex(i)
        p._on_preset(i)
        p.map.zoom = 15
        p.map.update()
        if area_name:
            p.name.setText(area_name)
        log("preset " + preset)

    def set_mode():
        p = holder["p"]
        p.mode.showPopup()

    def pick_mode():
        p = holder["p"]
        p.mode.hidePopup()
        p.mode.setCurrentIndex(p.mode.findData(mode))
        p._on_mode()
        log("mode " + mode)

    def show_mode():
        p = holder["p"]
        p._update_estimate()

    def go():
        p = holder["p"]
        p.go.click()
        log("fetch & build clicked")

    def fetch_done():
        p = holder["p"]
        return p._thread is None and ("Done" in p.status.text() or "Failed" in p.status.text())

    def post_build():
        p = holder["p"]
        holder["node"] = p.node
        p.node.parent().setName(node_name, unique_name=True)
        p.node.parent().setDisplayFlag(True)
        log("built: " + p.status.text())

    def shrink_panel():
        p = holder["p"]
        p.showMinimized()
        v = _viewer()
        v.curViewport().setCamera(cam)
        v.setCurrentNode(holder["node"])
        net = hou.ui.paneTabOfType(hou.paneTabType.NetworkEditor)
        if net:
            net.setPwd(holder["node"].parent())
            net.homeToSelection() if hasattr(net, "homeToSelection") else None
        holder["node"].setSelected(True, clear_all_selected=True)
        log("viewport")

    def dive_network():
        net = hou.ui.paneTabOfType(hou.paneTabType.NetworkEditor)
        if net:
            net.setPwd(holder["node"])
            for n in holder["node"].children():
                n.setSelected(n.name() in highlight, clear_all_selected=False)
            try:
                net.frameSelection()
            except Exception:
                pass
        log("network")

    def back_up():
        net = hou.ui.paneTabOfType(hou.paneTabType.NetworkEditor)
        if net:
            net.setPwd(holder["node"].parent())
        holder["node"].setSelected(True, clear_all_selected=True)

    centre = (0.0, 0.0, 0.0)
    steps = [("do", lambda: activate(HOUDINI_APP), 1500),
             ("do", open_panel, 2500),
             ("do", pick_preset, 3000)]
    p_ref = lambda: holder["p"]
    if mode == "google3d":
        steps += [("do", set_mode, 1800), ("do", pick_mode, 2500)]
    else:
        steps += [("do", lambda: p_ref().terrain.showPopup(), 1400), ("do", lambda: p_ref().terrain.hidePopup(), 300),
                  ("do", lambda: p_ref().imagery.showPopup(), 1400), ("do", lambda: p_ref().imagery.hidePopup(), 300),
                  ("do", lambda: p_ref().bsrc.showPopup(), 1600), ("do", lambda: p_ref().bsrc.hidePopup(), 300)]
    steps += [("do", show_mode, 1200),
              ("do", go, 500),
              ("wait", fetch_done, 400),
              ("do", post_build, 2500),
              ("do", shrink_panel, 1200)]
    steps += _cam_orbit(cam, centre, 900, 520, 200, 290, 14)
    steps += [("do", dive_network, 4500), ("do", back_up, 500)]
    steps += _cam_orbit(cam, (0, 0, 0), 420, 260, 290, 340, 8, target_h=200)
    tl = Timeline(steps)
    _TL["tl"] = tl
    tl.start()
    return True
