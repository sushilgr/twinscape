"""Twinscape PySide6 panel: pick an area on a map, choose sources, ingest, build, export.

Open from the HDA ("Open Twinscape Panel") or:  import earthtwin.ui as u; u.show()
"""
import io
import math
import os
import threading

import hou
from PySide6 import QtCore, QtGui, QtWidgets

from . import geo, hda, ingest, net, paths, providers

_PANEL = None
PREVIEW_URL = "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}"

STYLE = """
QWidget#Twinscape { background:#1e2124; color:#d8dadc; }
QGroupBox { border:1px solid #33383d; border-radius:6px; margin-top:14px; padding:8px 8px 6px 8px; font-weight:600; }
QGroupBox::title { subcontrol-origin: margin; left:10px; padding:0 4px; color:#8fb8de; }
QPushButton { background:#2c3136; border:1px solid #3c4248; border-radius:4px; padding:6px 10px; }
QPushButton:hover { background:#353b41; }
QPushButton#primary { background:#2f6fa8; border-color:#3a82c4; color:white; font-weight:600; }
QPushButton#primary:hover { background:#3a82c4; }
QPushButton:disabled { color:#6b7178; }
QLineEdit, QDoubleSpinBox, QSpinBox, QComboBox { background:#15181a; border:1px solid #33383d; border-radius:3px; padding:3px 5px; }
QProgressBar { border:1px solid #33383d; border-radius:3px; text-align:center; background:#15181a; height:16px; }
QProgressBar::chunk { background:#2f6fa8; }
QLabel#hint { color:#8a9097; font-size:11px; }
QLabel#title { font-size:16px; font-weight:700; color:#e9ecef; }
QPlainTextEdit { background:#15181a; border:1px solid #33383d; color:#a9b0b7; font-family:Menlo,monospace; font-size:11px; }
"""


# ======================================================================= map
class MapView(QtWidgets.QWidget):
    """Slippy-map preview with the capture square. Click to recentre, wheel to zoom,
    drag to pan."""
    centreChanged = QtCore.Signal(float, float)
    _tileReady = QtCore.Signal(int, int, int, bytes)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(380, 320)
        self.lat, self.lon, self.size, self.zoom = 40.708, -74.0115, 1000.0, 15
        self.tiles = {}
        self.pending = set()
        self.cache = os.path.join(ingest.DEFAULTS["cache_dir"], "_preview")
        self._drag = None
        self._tileReady.connect(self._on_tile)
        self.setMouseTracking(True)
        self.setCursor(QtCore.Qt.CrossCursor)

    def set_area(self, lat, lon, size):
        self.lat, self.lon, self.size = lat, lon, size
        self.update()

    def _gp(self):
        return geo.ll_to_tile(self.lat, self.lon, self.zoom)

    def _fetch(self, z, x, y):
        key = (z, x, y)
        if key in self.tiles or key in self.pending:
            return
        self.pending.add(key)

        def run():
            try:
                data = net.cached_get(PREVIEW_URL.format(z=z, x=x, y=y),
                                      os.path.join(self.cache, str(z), str(x), "%d.jpg" % y), ok_404=True)
            except Exception:
                data = b""
            self._tileReady.emit(z, x, y, data or b"")
        threading.Thread(target=run, daemon=True).start()

    @QtCore.Slot(int, int, int, bytes)
    def _on_tile(self, z, x, y, data):
        self.pending.discard((z, x, y))
        img = QtGui.QImage()
        img.loadFromData(data)
        self.tiles[(z, x, y)] = img
        self.update()

    def paintEvent(self, ev):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        p.fillRect(self.rect(), QtGui.QColor("#101214"))
        w, h = self.width(), self.height()
        cx, cy = self._gp()
        n = 2 ** self.zoom
        x0, y0 = cx - w / 512.0, cy - h / 512.0
        for ty in range(int(math.floor(y0)), int(math.floor(cy + h / 512.0)) + 1):
            for tx in range(int(math.floor(x0)), int(math.floor(cx + w / 512.0)) + 1):
                if ty < 0 or ty >= n:
                    continue
                key = (self.zoom, tx % n, ty)
                img = self.tiles.get(key)
                sx, sy = (tx - x0) * 256, (ty - y0) * 256
                if img is None:
                    self._fetch(*key)
                elif not img.isNull():
                    p.drawImage(QtCore.QRectF(sx, sy, 256, 256), img)
        # capture square
        mpp = geo.ground_res(self.lat, self.zoom)
        side = self.size / mpp
        r = QtCore.QRectF(w / 2 - side / 2, h / 2 - side / 2, side, side)
        p.fillRect(r, QtGui.QColor(47, 111, 168, 50))
        p.setPen(QtGui.QPen(QtGui.QColor("#5aa9ff"), 2))
        p.drawRect(r)
        p.setPen(QtGui.QPen(QtGui.QColor("#ffffff"), 1))
        p.drawLine(w / 2 - 6, h / 2, w / 2 + 6, h / 2)
        p.drawLine(w / 2, h / 2 - 6, w / 2, h / 2 + 6)
        p.fillRect(QtCore.QRectF(0, h - 18, w, 18), QtGui.QColor(0, 0, 0, 140))
        p.setPen(QtGui.QColor("#c8ccd0"))
        p.drawText(QtCore.QRectF(6, h - 18, w - 12, 18), QtCore.Qt.AlignVCenter | QtCore.Qt.AlignLeft,
                   "%.5f, %.5f   %.0f m   z%d   click: centre  drag: pan  wheel: zoom" %
                   (self.lat, self.lon, self.size, self.zoom))
        p.drawText(QtCore.QRectF(6, h - 18, w - 12, 18), QtCore.Qt.AlignVCenter | QtCore.Qt.AlignRight,
                   "Preview: Esri World Imagery")

    def _px_to_ll(self, pos):
        cx, cy = self._gp()
        tx = cx + (pos.x() - self.width() / 2.0) / 256.0
        ty = cy + (pos.y() - self.height() / 2.0) / 256.0
        n = 2 ** self.zoom
        lon = tx / n * 360.0 - 180.0
        lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * ty / n))))
        return lat, lon

    def ll_to_px(self, lat, lon):
        """Widget pixel of a lat/lon at the current view."""
        cx, cy = self._gp()
        tx, ty = geo.ll_to_tile(lat, lon, self.zoom)
        return QtCore.QPointF(self.width() / 2.0 + (tx - cx) * 256.0, self.height() / 2.0 + (ty - cy) * 256.0)

    def click_at(self, pos):
        """Recentre on a widget pixel (same as a mouse click)."""
        self.lat, self.lon = self._px_to_ll(pos)
        self.update()
        self.centreChanged.emit(self.lat, self.lon)

    def mousePressEvent(self, e):
        self._drag = (e.position(), self.lat, self.lon, False)

    def mouseMoveEvent(self, e):
        if self._drag and e.buttons():
            start, lat0, lon0, _ = self._drag
            d = e.position() - start
            if abs(d.x()) + abs(d.y()) > 3:
                n = 2 ** self.zoom
                cx, cy = geo.ll_to_tile(lat0, lon0, self.zoom)
                tx, ty = cx - d.x() / 256.0, cy - d.y() / 256.0
                self.lon = tx / n * 360.0 - 180.0
                self.lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * ty / n))))
                self._drag = (start, lat0, lon0, True)
                self.update()

    def mouseReleaseEvent(self, e):
        if self._drag and not self._drag[3]:
            self.lat, self.lon = self._px_to_ll(e.position())
        self._drag = None
        self.update()
        self.centreChanged.emit(self.lat, self.lon)

    def wheelEvent(self, e):
        self.zoom = int(max(3, min(19, self.zoom + (1 if e.angleDelta().y() > 0 else -1))))
        self.update()


# ======================================================================= worker
class FetchWorker(QtCore.QObject):
    progress = QtCore.Signal(float, str)
    finished = QtCore.Signal(object, str)

    def __init__(self, params, force):
        super().__init__()
        self.params, self.force = params, force

    def run(self):
        try:
            man = ingest.fetch(self.params, progress=lambda f, m: self.progress.emit(f, m), force=self.force)
            self.finished.emit(man, "")
        except Exception as ex:  # surface to UI, never the key
            self.finished.emit(None, str(ex))


# ======================================================================= panel
def _combo(items):
    c = QtWidgets.QComboBox()
    for key, label in items:
        c.addItem(label, key)
    return c


def _spin(lo, hi, val, dec=2, step=1.0, suffix=""):
    s = QtWidgets.QDoubleSpinBox()
    s.setRange(lo, hi)
    s.setDecimals(dec)
    s.setSingleStep(step)
    s.setValue(val)
    if suffix:
        s.setSuffix(suffix)
    return s


class TwinscapePanel(QtWidgets.QWidget):
    def __init__(self, node=None, parent=None):
        super().__init__(parent, QtCore.Qt.Window)
        self.setObjectName("Twinscape")
        self.setWindowTitle("Twinscape  -  Real-world digital twins for Houdini & Unreal")
        self.setStyleSheet(STYLE)
        self.resize(980, 760)
        self.node = node
        self._thread = None
        self._build()
        if node:
            self._pull_from_node()
        self._refresh_cached()

    # ---------------------------------------------------------------- layout
    def _build(self):
        root = QtWidgets.QHBoxLayout(self)
        left = QtWidgets.QVBoxLayout()
        right = QtWidgets.QVBoxLayout()
        root.addLayout(left, 3)
        root.addLayout(right, 2)

        t = QtWidgets.QLabel("Twinscape")
        t.setObjectName("title")
        self.target = QtWidgets.QLabel("")
        self.target.setObjectName("hint")
        hdr = QtWidgets.QHBoxLayout()
        hdr.addWidget(t)
        hdr.addStretch(1)
        hdr.addWidget(self.target)
        left.addLayout(hdr)

        self.map = MapView()
        self.map.centreChanged.connect(self._on_map_centre)
        left.addWidget(self.map, 1)

        loc = QtWidgets.QGroupBox("Location")
        g = QtWidgets.QGridLayout(loc)
        self.preset = QtWidgets.QComboBox()
        self.preset.addItem("Presets...")
        self.preset.addItems(list(ingest.PRESETS.keys()))
        self.preset.activated.connect(self._on_preset)
        self.name = QtWidgets.QLineEdit("Lower Manhattan")
        self.lat = _spin(-85, 85, 40.708, 6, 0.0005)
        self.lon = _spin(-180, 180, -74.0115, 6, 0.0005)
        self.size = _spin(100, 5000, 1000, 0, 50, " m")
        self.paste = QtWidgets.QLineEdit()
        self.paste.setPlaceholderText("Paste 'lat, lon' or a Google Maps URL and press Enter")
        self.paste.returnPressed.connect(self._on_paste)
        for w in (self.lat, self.lon, self.size):
            w.valueChanged.connect(self._sync_map)
        g.addWidget(self.preset, 0, 0, 1, 4)
        g.addWidget(QtWidgets.QLabel("Name"), 1, 0)
        g.addWidget(self.name, 1, 1, 1, 3)
        g.addWidget(QtWidgets.QLabel("Lat"), 2, 0)
        g.addWidget(self.lat, 2, 1)
        g.addWidget(QtWidgets.QLabel("Lon"), 2, 2)
        g.addWidget(self.lon, 2, 3)
        g.addWidget(QtWidgets.QLabel("Size"), 3, 0)
        g.addWidget(self.size, 3, 1)
        g.addWidget(self.paste, 4, 0, 1, 4)
        left.addWidget(loc)

        # ---- sources
        src = QtWidgets.QGroupBox("Sources")
        f = QtWidgets.QFormLayout(src)
        self.mode = QtWidgets.QComboBox()
        self.mode.addItem("Open data: terrain + imagery + extruded buildings", "procedural")
        self.mode.addItem("Google Photorealistic 3D Tiles (API key, look-dev only)", "google3d")
        self.mode.currentIndexChanged.connect(self._on_mode)
        self.terrain = _combo(providers.TERRAIN_SOURCES)
        self.imagery = _combo(providers.IMAGERY_SOURCES)
        self.bsrc = _combo(providers.BUILDING_SOURCES)
        self.key = QtWidgets.QLineEdit()
        self.key.setEchoMode(QtWidgets.QLineEdit.Password)
        self.key.setPlaceholderText("saved key found" if ingest.resolve_api_key("") else
                                    "Google Maps Platform key (or ~/.twinscape/google_api_key)")
        self.remember = QtWidgets.QCheckBox("Remember in ~/.twinscape")
        kh = QtWidgets.QHBoxLayout()
        kh.addWidget(self.key, 1)
        kh.addWidget(self.remember)
        f.addRow("Mode", self.mode)
        f.addRow("Terrain", self.terrain)
        f.addRow("Imagery", self.imagery)
        f.addRow("Buildings", self.bsrc)
        f.addRow("API key", kh)
        self.licence = QtWidgets.QLabel("")
        self.licence.setObjectName("hint")
        self.licence.setWordWrap(True)
        f.addRow(self.licence)
        right.addWidget(src)

        # ---- resolution
        res = QtWidgets.QGroupBox("Resolution")
        f2 = QtWidgets.QFormLayout(res)
        self.dem_res = _spin(0.5, 30, 2.0, 1, 0.5, " m")
        self.ortho_res = _spin(0.1, 10, 0.3, 2, 0.05, " m/px")
        self.max_tex = QtWidgets.QComboBox()
        self.max_tex.addItems(["2048", "4096", "8192", "16384"])
        self.max_tex.setCurrentText("8192")
        self.g3d_err = _spin(0.5, 64, 6.0, 1, 0.5, " m")
        self.g3d_fine = _spin(0.0, 16, 2.5, 2, 0.25, " m")
        self.g3d_fine.setToolTip("Street detail: tiles near the centre refine to this error (0 = uniform)")
        self.g3d_rad = _spin(10, 2000, 150, 0, 25, " m")
        self.g3d_rad.setToolTip("Street detail radius around the centre; detail falls off with distance beyond it")
        f2.addRow("Terrain spacing", self.dem_res)
        f2.addRow("Imagery", self.ortho_res)
        f2.addRow("Max texture", self.max_tex)
        f2.addRow("3D Tiles error", self.g3d_err)
        f2.addRow("Street detail", self.g3d_fine)
        f2.addRow("Detail radius", self.g3d_rad)
        self.estimate = QtWidgets.QLabel("")
        self.estimate.setObjectName("hint")
        f2.addRow(self.estimate)
        for w in (self.dem_res, self.ortho_res, self.size, self.g3d_err, self.g3d_fine, self.g3d_rad):
            w.valueChanged.connect(self._update_estimate)
        self.max_tex.currentIndexChanged.connect(self._update_estimate)
        self.terrain.currentIndexChanged.connect(self._update_estimate)
        right.addWidget(res)

        # ---- process
        pro = QtWidgets.QGroupBox("Processing / Unreal")
        f3 = QtWidgets.QFormLayout(pro)
        self.hscale = _spin(0, 10, 1.0, 2, 0.1)
        self.bscale = _spin(0.1, 5, 1.0, 2, 0.1)
        self.chunk = _spin(50, 2000, 250, 0, 50, " m")
        self.nanite = QtWidgets.QCheckBox("Nanite")
        self.nanite.setChecked(True)
        self.collision = QtWidgets.QCheckBox("Complex collision")
        self.collision.setChecked(True)
        ch = QtWidgets.QHBoxLayout()
        ch.addWidget(self.nanite)
        ch.addWidget(self.collision)
        f3.addRow("Terrain height x", self.hscale)
        f3.addRow("Building height x", self.bscale)
        f3.addRow("Chunk size", self.chunk)
        f3.addRow(ch)
        right.addWidget(pro)

        # ---- actions
        act = QtWidgets.QGroupBox("Build")
        v = QtWidgets.QVBoxLayout(act)
        self.go = QtWidgets.QPushButton("Fetch && Build")
        self.go.setObjectName("primary")
        self.go.clicked.connect(lambda: self._fetch(False))
        self.refetch = QtWidgets.QPushButton("Re-download")
        self.refetch.clicked.connect(lambda: self._fetch(True))
        self.usd = QtWidgets.QPushButton("Export USD")
        self.usd.clicked.connect(self._export_usd)
        self.folder = QtWidgets.QPushButton("Open folder")
        self.folder.clicked.connect(lambda: self.node and hda.on_open_folder({"node": self.node}))
        self.purge = QtWidgets.QPushButton("Delete Google data")
        self.purge.setToolTip("Removes every cached area built from Google content (3D Tiles mesh/atlas, satellite).")
        self.purge.clicked.connect(self._purge_google)
        row = QtWidgets.QHBoxLayout()
        row.addWidget(self.go, 2)
        row.addWidget(self.refetch, 1)
        row2 = QtWidgets.QHBoxLayout()
        row2.addWidget(self.usd)
        row2.addWidget(self.folder)
        row2.addWidget(self.purge)
        self.bar = QtWidgets.QProgressBar()
        self.bar.setRange(0, 1000)
        self.status = QtWidgets.QLabel("Ready")
        self.status.setObjectName("hint")
        self.cached = QtWidgets.QComboBox()
        self.cached.activated.connect(self._load_cached)
        v.addLayout(row)
        v.addLayout(row2)
        v.addWidget(self.bar)
        v.addWidget(self.status)
        v.addWidget(QtWidgets.QLabel("Cached areas"))
        v.addWidget(self.cached)
        right.addWidget(act)
        self.log = QtWidgets.QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(400)
        right.addWidget(self.log, 1)
        self._on_mode()
        self._update_estimate()

    # ---------------------------------------------------------------- helpers
    def _log(self, s):
        self.log.appendPlainText(s)

    def _sync_map(self):
        self.map.set_area(self.lat.value(), self.lon.value(), self.size.value())

    def _on_map_centre(self, lat, lon):
        for w, v in ((self.lat, lat), (self.lon, lon)):
            w.blockSignals(True)
            w.setValue(v)
            w.blockSignals(False)
        self._sync_map()

    def _on_preset(self, i):
        name = self.preset.currentText()
        if name in ingest.PRESETS:
            lat, lon, size = ingest.PRESETS[name]
            self.name.setText(name.split("(")[0].strip())
            self.lat.setValue(lat)
            self.lon.setValue(lon)
            self.size.setValue(size)
            self.map.zoom = 15 if size < 2500 else 14
            self._sync_map()

    def _on_paste(self):
        import re
        t = self.paste.text()
        m = re.search(r"@(-?\d+\.\d+),(-?\d+\.\d+)", t) or re.search(r"(-?\d+\.\d+)\s*[, ]\s*(-?\d+\.\d+)", t)
        if m:
            self.lat.setValue(float(m.group(1)))
            self.lon.setValue(float(m.group(2)))
            self.paste.clear()
        else:
            self.status.setText("Could not read coordinates from that text")

    def _on_mode(self, *_):
        g3d = self.mode.currentData() == "google3d"
        self.bsrc.setEnabled(not g3d)
        self.terrain.setEnabled(not g3d)
        self.imagery.setEnabled(not g3d)
        self.g3d_err.setEnabled(g3d)
        self.g3d_fine.setEnabled(g3d)
        self.g3d_rad.setEnabled(g3d)
        for w in (self.dem_res, self.ortho_res, self.max_tex, self.hscale, self.bscale):
            w.setEnabled(not g3d)        # open-data only settings
        self.licence.setText(
            "Google Map Tiles terms restrict caching, offline use and derived geometry. This mode stores the "
            "decoded mesh + texture atlas in the area folder so Houdini/Unreal can use it: keep it to internal "
            "look-dev your Google agreement allows, show Google attribution, and use 'Delete Google data' when done."
            if g3d else
            "USGS 3DEP and NAIP are public domain. NYC Open Data / OSM (ODbL) need attribution. "
            "Esri World Imagery requires an appropriate Esri licence for commercial use.")

    def _update_estimate(self, *_):
        s = self.size.value()
        n = min(2049, int(round(s / self.dem_res.value())) + 1)
        m = min(int(self.max_tex.currentText()), 2 ** int(round(math.log2(max(256.0, s / self.ortho_res.value())))))
        txt = "Terrain %d x %d (%.1fM tris)  |  ortho %d px = %.2f m/px" % (n, n, 2 * (n - 1) ** 2 / 1e6, m, s / max(1, m))
        if self.terrain.currentData() == "google_elevation" and self.mode.currentData() != "google3d":
            txt += "  |  Elevation API: %d requests" % ((n * n + 255) // 256)
        if self.mode.currentData() == "google3d":
            f, c, r = self.g3d_fine.value(), self.g3d_err.value(), self.g3d_rad.value()
            if f > 0 and f < c:
                txt = ("3D Tiles: %.1f m error within %.0f m of the centre, coarsening to %.1f m by %.0f m "
                       "(view-dependent, like a game streamer)" % (f, r, c, r * c / f))
            else:
                txt = "3D Tiles: uniform %.1f m error (no street detail focus)" % c
        self.estimate.setText(txt)

    def _set_combo(self, combo, key):
        i = combo.findData(key)
        if i >= 0:
            combo.setCurrentIndex(i)

    def _pull_from_node(self):
        n = self.node
        self.target.setText("Target: " + n.path())
        self.name.setText(n.evalParm("area_name"))
        self.lat.setValue(n.evalParm("lat"))
        self.lon.setValue(n.evalParm("lon"))
        self.size.setValue(n.evalParm("size"))
        self._set_combo(self.mode, n.parm("mode").evalAsString())
        self._set_combo(self.terrain, n.parm("terrain_src").evalAsString())
        self._set_combo(self.imagery, n.parm("imagery_src").evalAsString())
        self._set_combo(self.bsrc, n.parm("buildings_src").evalAsString())
        self.dem_res.setValue(n.evalParm("dem_res"))
        self.ortho_res.setValue(n.evalParm("ortho_res"))
        self.max_tex.setCurrentText(n.parm("max_tex").evalAsString())
        self.g3d_err.setValue(n.evalParm("g3d_error"))
        if n.parm("g3d_focus_error") is not None:
            self.g3d_fine.setValue(n.evalParm("g3d_focus_error"))
            self.g3d_rad.setValue(n.evalParm("g3d_focus_radius"))
        self.hscale.setValue(n.evalParm("height_scale"))
        self.bscale.setValue(n.evalParm("bld_height_scale"))
        self.chunk.setValue(n.evalParm("chunk_size"))
        self.nanite.setChecked(bool(n.evalParm("nanite")))
        self.collision.setChecked(bool(n.evalParm("collision")))
        self._sync_map()

    def _push_to_node(self):
        n = self.node
        with hou.undos.group("Twinscape panel"):
            n.parm("area_name").set(self.name.text())
            n.parm("lat").set(self.lat.value())
            n.parm("lon").set(self.lon.value())
            n.parm("size").set(self.size.value())
            n.parm("mode").set(self.mode.currentData())
            n.parm("terrain_src").set(self.terrain.currentData())
            n.parm("imagery_src").set(self.imagery.currentData())
            n.parm("buildings_src").set(self.bsrc.currentData())
            n.parm("dem_res").set(self.dem_res.value())
            n.parm("ortho_res").set(self.ortho_res.value())
            n.parm("max_tex").set(self.max_tex.currentText())
            n.parm("g3d_error").set(self.g3d_err.value())
            if n.parm("g3d_focus_error") is not None:
                n.parm("g3d_focus_error").set(self.g3d_fine.value())
                n.parm("g3d_focus_radius").set(self.g3d_rad.value())
            n.parm("height_scale").set(self.hscale.value())
            n.parm("bld_height_scale").set(self.bscale.value())
            n.parm("chunk_size").set(self.chunk.value())
            n.parm("nanite").set(int(self.nanite.isChecked()))
            n.parm("collision").set(int(self.collision.isChecked()))

    def _ensure_node(self):
        if self.node is not None:
            try:
                self.node.path()
                return self.node
            except hou.ObjectWasDeleted:
                self.node = None
        obj = hou.node("/obj")
        geo_ = obj.createNode("geo", "twinscape_" + hou.text.variableName(self.name.text()).lower())
        self.node = geo_.createNode("earthtwin::city_builder::1.0", "city_builder")
        # the panel owns fetching: never let a viewport cook start a second, blocking download
        self.node.parm("auto_fetch").set(0)
        self.node.setDisplayFlag(True)
        self.node.setRenderFlag(True)
        self.target.setText("Target: " + self.node.path())
        return self.node

    # ---------------------------------------------------------------- actions
    def _fetch(self, force):
        if self._thread:
            return
        node = self._ensure_node()
        self._push_to_node()
        if self.key.text() and self.remember.isChecked():
            d = os.path.dirname(paths.key_files()[0])
            os.makedirs(d, exist_ok=True)
            p = os.path.join(d, "google_api_key")
            with open(p, "w") as f:
                f.write(self.key.text().strip())
            os.chmod(p, 0o600)
        params = hda.node_params(node)
        params["api_key"] = self.key.text().strip()
        # keep Houdini responsive: no cook-triggered auto-fetch on the main thread while the
        # worker downloads (restored in _on_done)
        self._auto_fetch = node.evalParm("auto_fetch")
        node.parm("auto_fetch").set(0)
        self.go.setEnabled(False)
        self.refetch.setEnabled(False)
        self._log("Fetching %s (%.5f, %.5f, %.0f m)..." % (params["name"], params["lat"], params["lon"], params["size"]))
        self._thread = QtCore.QThread()
        self._worker = FetchWorker(params, force)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.progress.connect(self._on_progress)
        self._worker.finished.connect(self._on_done)
        self._thread.start()

    def _on_progress(self, f, msg):
        self.bar.setValue(int(f * 1000))
        self.status.setText(msg)

    def _on_done(self, man, err):
        self._thread.quit()
        self._thread.wait()
        self._thread = None
        self.go.setEnabled(True)
        self.refetch.setEnabled(True)
        try:
            if self.node is not None and getattr(self, "_auto_fetch", None) is not None:
                self.node.parm("auto_fetch").set(self._auto_fetch)
        except Exception:
            pass
        if err:
            self.status.setText("Failed: " + err)
            self._log("ERROR: " + err)
            return
        self._log("Ingested %s in %ss" % (man["area_id"], man.get("seconds")))
        for a in man.get("attribution", []):
            self._log("  " + a)
        self.status.setText("Building geometry...")
        QtWidgets.QApplication.processEvents()
        hda.apply_manifest(self.node, man)
        g = self.node.node("OUT").geometry()
        self._log("Built: %d prims, %d points" % (g.intrinsicValue("primitivecount"), g.intrinsicValue("pointcount")))
        self.status.setText("Done - %s" % man["area_id"])
        self.bar.setValue(1000)
        self._refresh_cached()

    def _refresh_cached(self):
        self.cached.clear()
        self._areas = ingest.list_areas()
        self.cached.addItem("(%d cached)" % len(self._areas))
        for a in self._areas:
            self.cached.addItem("%s  [%s]" % (a["area_id"], a["created"]))

    def _load_cached(self, i):
        if i <= 0:
            return
        man = self._areas[i - 1]
        p = man["params"]
        self.name.setText(p["name"])
        self.lat.setValue(p["lat"])
        self.lon.setValue(p["lon"])
        self.size.setValue(p["size"])
        self._set_combo(self.mode, p["mode"])
        self._set_combo(self.terrain, p["terrain_src"])
        self._set_combo(self.imagery, p["imagery_src"])
        self._set_combo(self.bsrc, p["buildings_src"])
        self.dem_res.setValue(p["dem_res"])
        self.ortho_res.setValue(p["ortho_res"])
        self.max_tex.setCurrentText(str(p["max_tex"]))
        node = self._ensure_node()
        self._push_to_node()
        hda.apply_manifest(node, man)
        self._log("Loaded cached area " + man["area_id"])

    def _purge_google(self):
        if QtWidgets.QMessageBox.question(self, "Twinscape", "Delete all cached areas built from Google content?") \
                != QtWidgets.QMessageBox.Yes:
            return
        gone = ingest.purge_google()
        self._log("Deleted Google-derived areas: %s" % (", ".join(gone) or "none"))
        self._refresh_cached()

    def _export_usd(self):
        if not self.node:
            return
        path = self.node.parm("usd_path").evalAsString() or hda.default_usd_path(self.node)
        n = hda.export_usd(self.node, path)
        self._log("USD: %s (%d meshes)" % (path, n))


EarthTwinPanel = TwinscapePanel   # backwards compatibility


def show(node=None):
    global _PANEL
    if _PANEL is not None:
        try:
            _PANEL.close()
        except Exception:
            pass
    _PANEL = TwinscapePanel(node, parent=hou.qt.mainWindow())
    _PANEL.show()
    _PANEL.raise_()
    return _PANEL
