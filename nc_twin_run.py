# nc_gui_host.py — NC GUI Host (supports HTML + CSS + JS + SVG via QtWebEngine)
# Runs nc_console.py and renders __TWIN__ commands as real windows (PySide6).
#
# Supports:
# - __TWIN__ + base64(json)  (t_windows format)
# - __TWIN__ + {json}        (plain json)
#
# Renders:
# - window.open / create / window
# - plot.add
# - table.set
# - t_windows: action=create/close/msgbox/init (+ content_html rendered)
#
# NEW (this version):
# - HTML tab gets a JS bridge (QtWebChannel) so JS can send messages to the Log tab:
#     ncSend("hello")  -> shows up as [js] hello
# - Optional per-window CSS + JS injection:
#     content_css, content_js
# - Optional command to run JS on the currently loaded HTML:
#     cmd/action: "html.eval" (or "js.eval") with field: code
# - --exe support: build a standalone executable that launches this GUI host
#   and still opens real TWIN / t_windows windows.
#
# NOTE:
# - If QtWebEngine is not installed, HTML falls back to QTextEdit.setHtml() (no JS).
#   Install typically: pip install PySide6 PySide6-QtWebEngine

from __future__ import annotations

import argparse
import contextlib
import io
import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from PySide6.QtCore import Qt, QProcess, QTimer, QRectF, QCoreApplication, QObject, Signal, Slot
from PySide6.QtGui import QPainter, QPen, QColor

# ---- WebEngine must be imported before QApplication is created (best practice) ----
QCoreApplication.setAttribute(Qt.AA_ShareOpenGLContexts, True)

WEBENGINE_AVAILABLE = False
try:
    from PySide6 import QtWebEngineWidgets  # noqa: F401
    from PySide6.QtWebEngineWidgets import QWebEngineView
    from PySide6.QtWebChannel import QWebChannel
    WEBENGINE_AVAILABLE = True
except Exception:
    QWebEngineView = None  # type: ignore
    QWebChannel = None  # type: ignore

from PySide6.QtWidgets import (
    QApplication,
    QMainWindow,
    QWidget,
    QVBoxLayout,
    QTextEdit,
    QLabel,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QComboBox,
    QMessageBox,
    QScrollArea,
    QFrame,
    QPushButton,
    QGraphicsOpacityEffect,
)

# ---- Optional Camera Support (QtMultimedia) ----
CAMERA_AVAILABLE = False
try:
    from PySide6.QtMultimedia import QMediaCaptureSession, QCamera, QImageCapture, QMediaRecorder
    from PySide6.QtMultimediaWidgets import QVideoWidget
    CAMERA_AVAILABLE = True
except Exception:
    QMediaCaptureSession = None  # type: ignore
    QCamera = None  # type: ignore
    QImageCapture = None  # type: ignore
    QMediaRecorder = None  # type: ignore
    QVideoWidget = None  # type: ignore

HERE = os.path.dirname(os.path.abspath(__file__))
THIS_FILE = os.path.abspath(__file__)
NC_CONSOLE = os.path.join(HERE, "nc_console.py")


def _is_url(s: str) -> bool:
    return str(s).startswith("https://") or str(s).startswith("http://")


def _resolve_local_target(target: str) -> str:
    """Resolve a relative target from the shell, then from NC's install folder."""
    if _is_url(target) or os.path.isabs(target):
        return target
    candidates = [Path.cwd() / target, Path(__file__).resolve().parent / target]
    if not target.lower().endswith((".nc", ".nce")):
        candidates.extend([
            Path.cwd() / (target + ".nc"),
            Path.cwd() / (target + ".nce"),
            Path(__file__).resolve().parent / (target + ".nc"),
            Path(__file__).resolve().parent / (target + ".nce"),
        ])
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate.resolve())
    return target


def _safe_exe_name_from_target(target: str) -> str:
    base = os.path.basename(target)
    if base.lower().endswith(".nc"):
        base = base[:-3]
    if base.lower().endswith(".nce"):
        base = base[:-4]
    base = base.strip() or "nc_twin_app"
    cleaned = []
    for ch in base:
        cleaned.append(ch if (ch.isalnum() or ch in ("_", "-", ".")) else "_")
    return "".join(cleaned).strip("._") or "nc_twin_app"


def _compute_base(target: str) -> str:
    if _is_url(target):
        return target.rsplit("/", 1)[0] + "/"
    return os.path.dirname(os.path.abspath(target)) or os.getcwd()


def _existing_search_paths(paths: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in paths or []:
        try:
            p = os.path.abspath(str(raw))
        except Exception:
            continue
        if not os.path.isdir(p):
            continue
        key = os.path.normcase(p)
        if key in seen:
            continue
        seen.add(key)
        out.append(p)
    return out


# -----------------------------
# TWIN parse
# -----------------------------

def _try_parse_twin(line: str) -> dict | None:
    if not line.startswith("__TWIN__"):
        return None
    payload = line[len("__TWIN__"):].strip()

    # 1) direct JSON
    if payload.startswith("{") and payload.endswith("}"):
        try:
            obj = json.loads(payload)
            return obj if isinstance(obj, dict) else None
        except Exception:
            return None

    # 2) base64(JSON)
    try:
        raw = base64.b64decode(payload.encode("ascii"), validate=False)
        obj = json.loads(raw.decode("utf-8", errors="replace"))
        return obj if isinstance(obj, dict) else None
    except Exception:
        return None


# -----------------------------
# Plot canvas (no external libs)
# -----------------------------

class PlotCanvas(QWidget):
    def __init__(self):
        super().__init__()
        self.series: dict[str, list[tuple[int, float]]] = {}
        self.max_points = 3000
        self.setMinimumHeight(260)

    def clear(self):
        self.series.clear()
        self.update()

    def add_point(self, series: str, step: int, value: float):
        series = str(series)
        pts = self.series.get(series)
        if pts is None:
            pts = []
            self.series[series] = pts
        pts.append((int(step), float(value)))
        if len(pts) > self.max_points:
            del pts[: len(pts) - self.max_points]
        self.update()

    def paintEvent(self, _ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)

        w = self.width()
        h = self.height()

        # background
        p.fillRect(0, 0, w, h, QColor(18, 18, 22))

        # margins
        L, R, T, B = 55, 12, 12, 28
        rect = QRectF(L, T, max(1, w - L - R), max(1, h - T - B))

        # axes box
        p.setPen(QPen(QColor(120, 120, 140), 1))
        p.drawRect(rect)

        if not self.series:
            p.setPen(QPen(QColor(180, 180, 200), 1))
            p.drawText(rect, Qt.AlignCenter, "No plot data yet…")
            return

        # bounds
        min_x = None
        max_x = None
        min_y = None
        max_y = None
        for pts in self.series.values():
            for x, y in pts:
                min_x = x if min_x is None else min(min_x, x)
                max_x = x if max_x is None else max(max_x, x)
                min_y = y if min_y is None else min(min_y, y)
                max_y = y if max_y is None else max(max_y, y)

        if min_x is None or max_x is None or min_y is None or max_y is None:
            return

        if min_x == max_x:
            max_x = min_x + 1
        if min_y == max_y:
            max_y = min_y + 1.0

        # add small y padding
        ypad = (max_y - min_y) * 0.05
        min_y -= ypad
        max_y += ypad

        def tx(x: int) -> float:
            return rect.left() + (x - min_x) / (max_x - min_x) * rect.width()

        def ty(y: float) -> float:
            return rect.bottom() - (y - min_y) / (max_y - min_y) * rect.height()

        # labels
        p.setPen(QPen(QColor(170, 170, 190), 1))
        p.drawText(8, 20, f"y: [{min_y:.3g} .. {max_y:.3g}]")
        p.drawText(8, h - 8, f"x: [{min_x} .. {max_x}]")

        # draw series
        for name, pts in self.series.items():
            if len(pts) < 2:
                continue

            # deterministic pseudo color from hash
            hv = abs(hash(name)) % 1000
            r = 80 + (hv % 150)
            g = 120 + ((hv // 3) % 120)
            b = 160 + ((hv // 7) % 95)
            color = QColor(r % 255, g % 255, b % 255)

            p.setPen(QPen(color, 2))
            x0, y0 = pts[0]
            for x1, y1 in pts[1:]:
                p.drawLine(int(tx(x0)), int(ty(y0)), int(tx(x1)), int(ty(y1)))
                x0, y0 = x1, y1


# -----------------------------
# HTML helpers (inject CSS/JS + WebChannel bridge)
# -----------------------------

_BRIDGE_INJECT = r"""
<script src="qrc:///qtwebchannel/qwebchannel.js"></script>
<script>
(function(){
  try{
    function init(){
      try{
        if (typeof QWebChannel === "undefined" || !window.qt || !qt.webChannelTransport) return;
        new QWebChannel(qt.webChannelTransport, function(channel){
          window.ncHost = channel.objects.ncHost || null;
          window.ncSend = function(msg){
            try{
              if (window.ncHost && window.ncHost.send) window.ncHost.send(String(msg));
            }catch(e){}
          };
          try{
            if (window.ncHost && window.ncHost.ready) window.ncHost.ready();
          }catch(e){}
        });
      }catch(e){}
    }
    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
    else init();
  }catch(e){}
})();
</script>
"""


def _insert_before_closing_head(doc: str, injection: str) -> str:
    lo = doc.lower()
    idx = lo.rfind("</head>")
    if idx >= 0:
        return doc[:idx] + injection + doc[idx:]
    return doc


def _wrap_as_document(body_html: str, extra_head: str) -> str:
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        + extra_head
        + "</head><body>"
        + body_html
        + "</body></html>"
    )


def _compose_html(html: str, extra_css: str = "", extra_js: str = "", enable_bridge: bool = True) -> str:
    html = html or ""
    extra_css = extra_css or ""
    extra_js = extra_js or ""

    head_bits = ""
    if extra_css.strip():
        head_bits += f"<style>\n{extra_css}\n</style>\n"
    if enable_bridge and WEBENGINE_AVAILABLE:
        head_bits += _BRIDGE_INJECT + "\n"
    if extra_js.strip():
        head_bits += f"<script>\n{extra_js}\n</script>\n"

    lo = html.lower()

    if "<html" in lo:
        if "<head" in lo:
            out = _insert_before_closing_head(html, head_bits)
            if out != html:
                return out
            return head_bits + html
        pos = lo.find("<html")
        gt = lo.find(">", pos) if pos >= 0 else -1
        if gt >= 0:
            return html[:gt + 1] + "<head>" + head_bits + "</head>" + html[gt + 1:]
        return _wrap_as_document(html, head_bits)

    return _wrap_as_document(html, head_bits)


class WebBridge(QObject):
    message = Signal(str)

    @Slot(str)
    def send(self, msg: str):
        self.message.emit(str(msg))

    @Slot()
    def ready(self):
        self.message.emit("[bridge] ready")


class TwinWindow(QMainWindow):
    def __init__(self, wid: str, title: str, w: int, h: int):
        super().__init__()
        self.wid = wid
        self.setWindowTitle(title)
        self.resize(w, h)

        root = QWidget()
        lay = QVBoxLayout(root)
        lay.setContentsMargins(10, 10, 10, 10)

        self.header = QLabel(f"NC Window: {title}")
        self.header.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.header.setStyleSheet("font-weight: 800; font-size: 16px;")

        self.tabs = QTabWidget()

        self.log = QTextEdit()
        self.log.setReadOnly(True)
        self.log.setPlaceholderText("Waiting for output...")
        self.tabs.addTab(self.log, "Log")

        plot_root = QWidget()
        plot_lay = QVBoxLayout(plot_root)
        plot_lay.setContentsMargins(8, 8, 8, 8)
        self.plot_canvas = PlotCanvas()
        plot_lay.addWidget(self.plot_canvas, 1)
        self.tabs.addTab(plot_root, "Plots")

        tables_root = QWidget()
        tables_lay = QVBoxLayout(tables_root)
        tables_lay.setContentsMargins(8, 8, 8, 8)
        self.table_selector = QComboBox()
        self.table_widget = QTableWidget()
        self.table_widget.setRowCount(0)
        self.table_widget.setColumnCount(2)
        self.table_widget.setHorizontalHeaderLabels(["C0", "C1"])
        self.table_widget.horizontalHeader().setStretchLastSection(True)
        self.tables: dict[str, list[list[object]]] = {}
        self.table_selector.currentTextChanged.connect(self._render_selected_table)
        tables_lay.addWidget(self.table_selector)
        tables_lay.addWidget(self.table_widget, 1)
        self.tabs.addTab(tables_root, "Tables")

        self._web_bridge = None
        self._web_channel = None
        if WEBENGINE_AVAILABLE:
            self.html = QWebEngineView()
            try:
                self._web_bridge = WebBridge()
                self._web_bridge.message.connect(lambda m: self.append_log(f"[js] {m}"))
                self._web_channel = QWebChannel(self.html.page())
                self._web_channel.registerObject("ncHost", self._web_bridge)
                self.html.page().setWebChannel(self._web_channel)
            except Exception as e:
                self.append_log(f"[warn] WebChannel init failed: {e}")
            self.html.setHtml(
                _compose_html(
                    "<div style='background:#111;color:#ddd;font-family:Segoe UI;padding:14px;'>No HTML content yet…</div>",
                    enable_bridge=True,
                )
            )
        else:
            self.html = QTextEdit()
            self.html.setReadOnly(True)
            self.html.setPlaceholderText("QtWebEngine missing: HTML shown without JS. Install PySide6-QtWebEngine.")
        self.tabs.addTab(self.html, "HTML")

        self.ui2_root = QWidget()
        self.ui2_lay = QVBoxLayout(self.ui2_root)
        self.ui2_lay.setContentsMargins(12, 12, 12, 12)
        self.ui2_lay.setSpacing(10)
        self.ui2_scroll = QScrollArea()
        self.ui2_scroll.setWidgetResizable(True)
        self.ui2_scroll.setFrameShape(QFrame.NoFrame)
        self.ui2_scroll.setWidget(self.ui2_root)
        self.tabs.addTab(self.ui2_scroll, "UI")

        cam_root = QWidget()
        cam_lay = QVBoxLayout(cam_root)
        cam_lay.setContentsMargins(8, 8, 8, 8)
        if CAMERA_AVAILABLE and QVideoWidget is not None:
            self._cam_video = QVideoWidget()
            cam_lay.addWidget(self._cam_video, 1)
        else:
            self._cam_video = None
            cam_lay.addWidget(QLabel("Camera not available"))
        self.tabs.addTab(cam_root, "Camera")

        self._cam_session = None
        self._cam = None
        self._cam_image = None
        self._cam_recorder = None

        lay.addWidget(self.header)
        lay.addWidget(self.tabs, 1)
        self.setCentralWidget(root)

        # HTML-only mode bugfix:
        # If a window is mainly used for content_html/ui.html_set(...),
        # the user may want to see only the rendered HTML without the
        # category tabs above it.
        self._html_only_mode = False
        try:
            self.tabs.tabBar().setVisible(True)
        except Exception:
            pass

    def _set_html_only_mode(self, enabled: bool):
        self._html_only_mode = bool(enabled)
        try:
            self.header.setVisible(not enabled)
        except Exception:
            pass
        try:
            self.tabs.tabBar().setVisible(not enabled)
        except Exception:
            pass
        try:
            if enabled:
                self.tabs.setCurrentWidget(self.html)
        except Exception:
            pass

    def set_title(self, title: str):
        self.setWindowTitle(title)
        self.header.setText(f"NC Window: {title}")

    def append_log(self, text: str):
        self.log.append(str(text))

    def add_plot_point(self, series: str, step: int, value: float):
        self._set_html_only_mode(False)
        self.plot_canvas.add_point(series, step, value)
        self.tabs.setCurrentIndex(self.tabs.indexOf(self.plot_canvas.parentWidget()))

    def set_table(self, name: str, rows: list):
        self._set_html_only_mode(False)
        key = str(name or "table")
        norm_rows: list[list[object]] = []
        for r in rows or []:
            if isinstance(r, (list, tuple)):
                norm_rows.append(list(r))
            else:
                norm_rows.append([r])
        self.tables[key] = norm_rows
        if self.table_selector.findText(key) < 0:
            self.table_selector.addItem(key)
        self.table_selector.setCurrentText(key)
        self._render_selected_table(key)
        self.tabs.setCurrentIndex(self.tabs.indexOf(self.table_widget.parentWidget()))

    def _render_selected_table(self, key: str):
        rows = self.tables.get(key, [])
        cols = max((len(r) for r in rows), default=0)
        self.table_widget.setRowCount(len(rows))
        self.table_widget.setColumnCount(max(1, cols))
        self.table_widget.setHorizontalHeaderLabels([f"C{i}" for i in range(max(1, cols))])
        for y, row in enumerate(rows):
            for x in range(max(1, cols)):
                val = row[x] if x < len(row) else ""
                self.table_widget.setItem(y, x, QTableWidgetItem(str(val)))

    def set_html(self, html: str, css: str = "", js: str = ""):
        doc = _compose_html(html, extra_css=css, extra_js=js, enable_bridge=True)
        if WEBENGINE_AVAILABLE and hasattr(self.html, "setHtml"):
            self.html.setHtml(doc)
        else:
            self.html.setHtml(doc)
        self._set_html_only_mode(True)
        self.tabs.setCurrentIndex(self.tabs.indexOf(self.html))

    def eval_js(self, code: str):
        if WEBENGINE_AVAILABLE and hasattr(self.html, "page"):
            try:
                self.html.page().runJavaScript(str(code))
            except Exception as e:
                self.append_log(f"[js.eval] failed: {e}")
        else:
            self.append_log("[js.eval] QtWebEngine not available")

    def bridge_post(self, payload):
        try:
            js = "try{ if(window.ncReceive) window.ncReceive(" + json.dumps(payload, ensure_ascii=False) + "); }catch(e){}"
            self.eval_js(js)
        except Exception as e:
            self.append_log(f"[bridge.post] failed: {e}")

    def ui2_set_scene(self, scene: dict):
        self._set_html_only_mode(False)
        try:
            while self.ui2_lay.count():
                item = self.ui2_lay.takeAt(0)
                w = item.widget()
                if w is not None:
                    w.deleteLater()
            nodes = scene.get("nodes") or []
            anims = scene.get("anims") or {}
            for n in nodes:
                if not isinstance(n, dict):
                    continue
                kind = str(n.get("type", "label"))
                if kind == "button":
                    w = QPushButton(str(n.get("text", "Button")))
                else:
                    w = QLabel(str(n.get("text", "")))
                props = n.get("props") or {}
                try:
                    col = props.get("color")
                    if isinstance(col, (list, tuple)) and len(col) >= 3:
                        qcol = QColor(int(col[0]), int(col[1]), int(col[2]))
                        pal = w.palette()
                        pal.setColor(w.foregroundRole(), qcol)
                        w.setPalette(pal)
                    op = props.get("opacity")
                    if op is not None:
                        eff = QGraphicsOpacityEffect()
                        eff.setOpacity(max(0.0, min(1.0, float(op))))
                        w.setGraphicsEffect(eff)
                except Exception:
                    pass
                self.ui2_lay.addWidget(w)
                anim_name = n.get("anim")
                if anim_name and isinstance(anims, dict) and anim_name in anims:
                    try:
                        eff = w.graphicsEffect()
                        if not isinstance(eff, QGraphicsOpacityEffect):
                            eff = QGraphicsOpacityEffect()
                            eff.setOpacity(0.0)
                            w.setGraphicsEffect(eff)
                        from PySide6.QtCore import QPropertyAnimation
                        anim = QPropertyAnimation(eff, b"opacity", w)
                        anim.setStartValue(float(anims[anim_name].get("opacity_from", 0.0)))
                        anim.setEndValue(float(anims[anim_name].get("opacity_to", 1.0)))
                        anim.setDuration(int(anims[anim_name].get("duration_ms", 600)))
                        anim.start()
                    except Exception:
                        pass
            self.tabs.setCurrentIndex(self.tabs.indexOf(self.ui2_scroll))
        except Exception as e:
            self.append_log(f"[ui2] render failed: {e}")

    def camera_open(self, facing: str = "back", w: int = 1280, h: int = 720, fps: int = 30):
        if not CAMERA_AVAILABLE:
            self.append_log("[camera] QtMultimedia not available.")
            return False
        try:
            if self._cam_session is None:
                self._cam_session = QMediaCaptureSession()
            if self._cam is None:
                self._cam = QCamera()
                self._cam_session.setCamera(self._cam)
            if self._cam_video is not None:
                self._cam_session.setVideoOutput(self._cam_video)
            if self._cam_image is None:
                self._cam_image = QImageCapture()
                self._cam_session.setImageCapture(self._cam_image)
            if self._cam_recorder is None:
                self._cam_recorder = QMediaRecorder()
                self._cam_session.setRecorder(self._cam_recorder)
            self._cam.start()
            self.tabs.setCurrentIndex(self.tabs.indexOf(self._cam_video.parentWidget() if self._cam_video else self.tabs.widget(4)))
            self.append_log(f"[camera] open facing={facing} {w}x{h}@{fps}")
            return True
        except Exception as e:
            self.append_log(f"[camera] open failed: {e}")
            return False

    def camera_close(self):
        try:
            if self._cam is not None:
                self._cam.stop()
            self.append_log("[camera] closed")
        except Exception as e:
            self.append_log(f"[camera] close failed: {e}")

    def camera_snap(self, path: str):
        if not CAMERA_AVAILABLE or self._cam_image is None:
            self.append_log("[camera] snap not available")
            return
        try:
            self._cam_image.captureToFile(str(path))
            self.append_log(f"[camera] snap -> {path}")
        except Exception as e:
            self.append_log(f"[camera] snap failed: {e}")

    def camera_record_start(self, path: str, w: int = 1280, h: int = 720, fps: int = 30):
        if not CAMERA_AVAILABLE or self._cam_recorder is None:
            self.append_log("[camera] recorder not available")
            return
        try:
            from PySide6.QtCore import QUrl
            self._cam_recorder.setOutputLocation(QUrl.fromLocalFile(str(path)))
            self._cam_recorder.record()
            self.append_log(f"[camera] record_start -> {path}")
        except Exception as e:
            self.append_log(f"[camera] record_start failed: {e}")

    def camera_record_stop(self):
        if not CAMERA_AVAILABLE or self._cam_recorder is None:
            self.append_log("[camera] recorder not available")
            return
        try:
            self._cam_recorder.stop()
            self.append_log("[camera] record_stop")
        except Exception as e:
            self.append_log(f"[camera] record_stop failed: {e}")

    def camera_set(self, key: str, value):
        self.append_log(f"[camera] set {key}={value} (not implemented)")


class HostApp:
    def __init__(self, host_argv: list[str], child_argv: list[str], target: str | None = None, base: str | None = None, search_paths: list[str] | None = None):
        self.app = QApplication.instance() or QApplication(host_argv)
        self.windows: dict[str, TwinWindow] = {}
        self.default_window_id = "nc_sim"
        self.proc = QProcess()
        self.target = str(target or "")
        self.base = str(base or (_compute_base(self.target) if self.target else os.getcwd()))
        self.search_paths = list(search_paths or [])

        if getattr(sys, "frozen", False):
            self.proc.setProgram(sys.executable)
            self.proc.setArguments(["--__nc_child__"] + list(child_argv))
            self.proc.setWorkingDirectory(os.getcwd())
        else:
            self.proc.setProgram(sys.executable)
            self.proc.setArguments([THIS_FILE, "--__nc_child__"] + list(child_argv))
            self.proc.setWorkingDirectory(HERE)

        self.proc.readyReadStandardOutput.connect(self._on_stdout)
        self.proc.readyReadStandardError.connect(self._on_stderr)
        self.proc.finished.connect(self._on_finished)

        self._buf_out = ""
        self._buf_err = ""

    def run(self) -> int:
        self.proc.start()
        if not self.proc.waitForStarted(5000):
            print("NCW GUI: Failed to start NC child process")
            return 2
        QTimer.singleShot(250, self._ensure_console_window)
        return self.app.exec()

    def _attach_bridge_handler(self, win: TwinWindow):
        try:
            if getattr(win, "_nc_bridge_hooked", False):
                return
            bridge = getattr(win, "_web_bridge", None)
            if bridge is None:
                return
            bridge.message.connect(lambda m, wid=win.wid: self._on_bridge_message(wid, m))
            win._nc_bridge_hooked = True
        except Exception:
            pass

    def _parse_bridge_message(self, msg: str):
        s = str(msg or "").strip()
        if not s:
            return ""
        if s[:1] in "[{":
            try:
                return json.loads(s)
            except Exception:
                return s
        return s

    def _append_to_window_log(self, wid: str, text: str, is_err: bool = False):
        line = str(text)
        if not line:
            return
        win = self.windows.get(str(wid)) or self._default_log_window()
        prefix = "[stderr] " if is_err else ""
        if win is not None:
            win.append_log(prefix + line)
        else:
            print(prefix + line)

    def _dispatch_inline_output(self, wid: str, text: str, is_err: bool = False) -> str:
        plain: list[str] = []
        for raw_line in str(text or "").splitlines():
            stripped = raw_line.strip()
            tw = _try_parse_twin(stripped)
            if tw is not None:
                self._handle_twin(tw)
            else:
                plain.append(raw_line)
                self._append_to_window_log(wid, raw_line, is_err=is_err)
        return "\n".join(plain)

    def _run_nc_snippet(self, wid: str, code: str, source_name: str = "<html>"):
        code = str(code or "")
        win = self.windows.get(str(wid)) or self._default_log_window()
        if not code.strip():
            self._append_to_window_log(wid, "[bridge] No NC code was provided.", is_err=True)
            if win is not None:
                win.bridge_post({"type": "nc_result", "ok": False, "stdout": "", "stderr": "No NC code was provided."})
            return
        try:
            import nc as _nc
        except Exception as e:
            self._append_to_window_log(wid, f"[bridge] Could not import NC: {e}", is_err=True)
            if win is not None:
                win.bridge_post({"type": "nc_result", "ok": False, "stdout": "", "stderr": f"NC import failed: {e}"})
            return

        out_buf = io.StringIO()
        err_buf = io.StringIO()
        ok = True
        try:
            with contextlib.redirect_stdout(out_buf), contextlib.redirect_stderr(err_buf):
                _nc.run_text(
                    code,
                    base=self.base,
                    extra_paths=list(self.search_paths),
                    policy=_nc.NCPolicy(),
                    enable_ui=True,
                    source_name=source_name,
                )
        except Exception as error:
            ok = False
            err_buf.write(_nc.format_exception(error) + "\n")

        stdout_text = self._dispatch_inline_output(wid, out_buf.getvalue(), is_err=False)
        stderr_text = self._dispatch_inline_output(wid, err_buf.getvalue(), is_err=True)

        if not stdout_text and not stderr_text:
            stdout_text = "[nc] Code executed."
            self._append_to_window_log(wid, stdout_text, is_err=False)

        if win is not None:
            win.bridge_post({
                "type": "nc_result",
                "ok": bool(ok and not stderr_text),
                "stdout": stdout_text,
                "stderr": stderr_text,
            })

    def _on_bridge_message(self, wid: str, msg: str):
        payload = self._parse_bridge_message(msg)
        if isinstance(payload, dict):
            cmd = str(payload.get("cmd") or payload.get("action") or "").strip().lower()
            if cmd in ("run_nc", "exec_nc", "run_code", "execute_nc"):
                source_name = str(payload.get("source_name") or (self.target + " [html]") or "<html>")
                self._run_nc_snippet(wid, str(payload.get("code") or ""), source_name=source_name)
                return
        if str(msg).strip() == "[bridge] ready":
            return

    def _ensure_console_window(self):
        if self.default_window_id in self.windows:
            return
        if self.windows and self.default_window_id not in self.windows:
            self.default_window_id = next(iter(self.windows.keys()))
            return
        w = TwinWindow(self.default_window_id, "NC Output", 1000, 700)
        w.show()
        self.windows[self.default_window_id] = w
        self._attach_bridge_handler(w)
        if not WEBENGINE_AVAILABLE:
            w.append_log("[info] QtWebEngine not available => HTML shows without JS. Install PySide6-QtWebEngine.")
        else:
            w.append_log("[info] HTML tab supports JS. In HTML you can call: ncSend('hi')")

    def _default_log_window(self):
        self._ensure_console_window()
        win = self.windows.get(self.default_window_id)
        if win is None and self.windows:
            win = next(iter(self.windows.values()))
            for k, v in self.windows.items():
                if v is win:
                    self.default_window_id = k
                    break
        return win

    def _get_or_create_window(self, wid: str, title: str, w: int, h: int) -> TwinWindow:
        wid = str(wid)
        title = str(title)
        win = self.windows.get(wid)
        if win is None:
            win = TwinWindow(wid, title, w, h)
            self.windows[wid] = win
            win.show()
            if not WEBENGINE_AVAILABLE:
                win.append_log("[info] QtWebEngine not available => HTML shows without JS. Install PySide6-QtWebEngine.")
            else:
                win.append_log("[info] HTML tab supports JS. In HTML you can call: ncSend('hi')")
        else:
            win.set_title(title)
            win.resize(w, h)
            win.show()
        self._attach_bridge_handler(win)
        win.raise_()
        win.activateWindow()
        return win

    def _msgbox(self, kind: str, title: str, message: str, default: bool = False):
        mb = QMessageBox()
        mb.setWindowTitle(title or "Message")
        mb.setText(message or "")
        k = (kind or "").lower()
        if k == "info":
            mb.setIcon(QMessageBox.Information)
            mb.setStandardButtons(QMessageBox.Ok)
        elif k == "warning":
            mb.setIcon(QMessageBox.Warning)
            mb.setStandardButtons(QMessageBox.Ok)
        elif k == "error":
            mb.setIcon(QMessageBox.Critical)
            mb.setStandardButtons(QMessageBox.Ok)
        elif k == "askyesno":
            mb.setIcon(QMessageBox.Question)
            mb.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
            mb.setDefaultButton(QMessageBox.Yes if default else QMessageBox.No)
        else:
            mb.setIcon(QMessageBox.Information)
            mb.setStandardButtons(QMessageBox.Ok)
        return mb.exec()

    def _handle_twin(self, cmd: dict):
        c = cmd.get("cmd") or cmd.get("action")
        if c in ("window.open", "create", "window"):
            wid = str(cmd.get("id", self.default_window_id))
            title = str(cmd.get("title", "NC"))
            w = int(cmd.get("w", 1000))
            h = int(cmd.get("h", 700))
            win = self._get_or_create_window(wid, title, w, h)
            content_html = cmd.get("content_html")
            content_css = cmd.get("content_css", "")
            content_js = cmd.get("content_js", "")
            if isinstance(content_html, str) and content_html.strip():
                win.set_html(content_html, css=str(content_css or ""), js=str(content_js or ""))
            return
        if c in ("window.close", "close"):
            wid = str(cmd.get("id", self.default_window_id))
            win = self.windows.pop(wid, None)
            if win:
                win.close()
            return
        if c == "init":
            self._ensure_console_window()
            self.windows[self.default_window_id].append_log(f"[init] {cmd}")
            wins = cmd.get("windows")
            if isinstance(wins, list):
                for wobj in wins:
                    if isinstance(wobj, dict):
                        w_id = str(wobj.get("id", self.default_window_id))
                        title = str(wobj.get("title", "TWIN"))
                        ww = int(wobj.get("w", 1000))
                        hh = int(wobj.get("h", 700))
                        win = self._get_or_create_window(w_id, title, ww, hh)
                        html = wobj.get("content_html")
                        css = wobj.get("content_css", "")
                        js = wobj.get("content_js", "")
                        if isinstance(html, str) and html.strip():
                            win.set_html(html, css=str(css or ""), js=str(js or ""))
            return
        if c == "msgbox":
            kind = str(cmd.get("kind", "info"))
            title = str(cmd.get("title", "Message"))
            message = str(cmd.get("message", ""))
            default = bool(cmd.get("default", False))
            self._msgbox(kind, title, message, default=default)
            return
        if c in ("ui.scene", "ui2.scene", "scene.set"):
            wid = str(cmd.get("id", self.default_window_id))
            title = str(cmd.get("title", self.windows.get(wid).windowTitle() if wid in self.windows else "NC"))
            win = self._get_or_create_window(wid, title, 1000, 700)
            scene = cmd.get("scene", cmd.get("ui", {}))
            if isinstance(scene, dict):
                win.ui2_set_scene(scene)
            else:
                win.append_log(f"[ui.scene] bad scene type: {type(scene)}")
            return
        if c in ("html.set", "window.html", "html"):
            wid = str(cmd.get("id", self.default_window_id))
            title = str(cmd.get("title", self.windows.get(wid).windowTitle() if wid in self.windows else "NC"))
            win = self._get_or_create_window(wid, title, 1000, 700)
            html = str(cmd.get("html", cmd.get("content_html", "")) or "")
            css = str(cmd.get("css", cmd.get("content_css", "")) or "")
            js = str(cmd.get("js", cmd.get("content_js", "")) or "")
            win.set_html(html, css=css, js=js)
            return
        if c in ("html.eval", "js.eval", "window.eval_js"):
            wid = str(cmd.get("id", self.default_window_id))
            win = self.windows.get(wid) or self.windows.get(self.default_window_id)
            if win is None:
                self._ensure_console_window()
                win = self.windows[self.default_window_id]
            code = str(cmd.get("code", cmd.get("js", "")) or "")
            if not code.strip():
                win.append_log("[warn] html.eval missing 'code'")
                return
            win.eval_js(code)
            return
        if c == "table.set":
            wid = str(cmd.get("id", self.default_window_id))
            name = str(cmd.get("name", "table"))
            rows = cmd.get("rows", [])
            win = self._get_or_create_window(wid, self.windows.get(wid).windowTitle() if wid in self.windows else "NC", 1000, 700)
            if isinstance(rows, list):
                win.set_table(name, rows)
            else:
                win.append_log(f"[table.set] bad rows type: {type(rows)}")
            return
        if c == "plot.add":
            wid = str(cmd.get("id", self.default_window_id))
            series = str(cmd.get("series", "series"))
            step = int(cmd.get("step", 0))
            value = float(cmd.get("value", 0.0))
            win = self._get_or_create_window(wid, self.windows.get(wid).windowTitle() if wid in self.windows else "NC", 1000, 700)
            win.add_plot_point(series, step, value)
            return
        if c in ("camera.open", "camera.close", "camera.snap", "camera.record_start", "camera.record_stop", "camera.set"):
            wid = str(cmd.get("id", self.default_window_id))
            title = self.windows.get(wid).windowTitle() if wid in self.windows else "NC Camera"
            win = self._get_or_create_window(wid, title, 1000, 700)
            if c == "camera.open":
                facing = str(cmd.get("facing", "back"))
                ww = int(cmd.get("w", 1280))
                hh = int(cmd.get("h", 720))
                fps = int(cmd.get("fps", 30))
                win.camera_open(facing=facing, w=ww, h=hh, fps=fps)
                return
            if c == "camera.close":
                win.camera_close()
                return
            if c == "camera.snap":
                path = str(cmd.get("path", "snap.jpg"))
                win.camera_snap(path)
                return
            if c == "camera.record_start":
                path = str(cmd.get("path", "video.mp4"))
                ww = int(cmd.get("w", 1280))
                hh = int(cmd.get("h", 720))
                fps = int(cmd.get("fps", 30))
                win.camera_record_start(path, w=ww, h=hh, fps=fps)
                return
            if c == "camera.record_stop":
                win.camera_record_stop()
                return
            if c == "camera.set":
                key = str(cmd.get("key", ""))
                val = cmd.get("value")
                win.camera_set(key, val)
                return
        self._ensure_console_window()
        self.windows[self.default_window_id].append_log(f"[TWIN] {cmd}")

    def _consume_lines(self, chunk: str, is_err: bool):
        buf = self._buf_err if is_err else self._buf_out
        buf += chunk
        lines = buf.splitlines(keepends=False)
        if buf and not buf.endswith("\n") and lines:
            buf = lines.pop()
        else:
            buf = ""
        if is_err:
            self._buf_err = buf
        else:
            self._buf_out = buf
        for line in lines:
            stripped = line.strip()
            tw = _try_parse_twin(stripped)
            if tw is not None:
                self._handle_twin(tw)
            else:
                self._ensure_console_window()
                prefix = "[stderr] " if is_err else ""
                win = self._default_log_window()
                if win is not None:
                    win.append_log(prefix + line)
                else:
                    print(prefix + line)

    def _on_stdout(self):
        data = bytes(self.proc.readAllStandardOutput()).decode("utf-8", errors="replace")
        self._consume_lines(data, is_err=False)

    def _on_stderr(self):
        data = bytes(self.proc.readAllStandardError()).decode("utf-8", errors="replace")
        self._consume_lines(data, is_err=True)

    def _on_finished(self, code: int, status):
        self._ensure_console_window()
        win = self.windows.get(self.default_window_id)
        if win is None and self.windows:
            win = next(iter(self.windows.values()))
        if win is not None:
            win.append_log(f"\n[NCW GUI] process finished (code={code})")
        else:
            print(f"[NCW GUI] process finished (code={code})")


def _candidate_pyinstaller_commands() -> list[list[str]]:
    candidates: list[list[str]] = []
    seen: set[tuple[str, ...]] = set()

    def add(cmd: list[str]) -> None:
        key = tuple(str(x) for x in cmd if str(x))
        if not key or key in seen:
            return
        seen.add(key)
        candidates.append(list(key))

    for name in ("pyinstaller", "pyinstaller.exe"):
        exe = shutil.which(name)
        if exe:
            add([exe])

    probe_roots = []
    for raw in (sys.executable, getattr(sys, "_base_executable", ""), sys.prefix, getattr(sys, "base_prefix", "")):
        if raw:
            probe_roots.append(Path(raw).resolve())

    for root in probe_roots:
        parts = [root]
        if root.is_file():
            parts.extend([root.parent, root.parent / "Scripts", root.parent.parent / "Scripts"])
        else:
            parts.extend([root / "Scripts", root / "bin"])
        for folder in parts:
            try:
                folder = Path(folder)
            except Exception:
                continue
            for name in ("pyinstaller.exe", "pyinstaller"):
                cand = folder / name
                if cand.is_file():
                    add([str(cand)])

    add([sys.executable, "-m", "pyinstaller"])

    py_launcher = shutil.which("py") or shutil.which("py.exe")
    if py_launcher:
        add([py_launcher, "-m", "pyinstaller"])

    return candidates


def _resolve_pyinstaller_command() -> tuple[list[str], list[str]]:
    attempts: list[str] = []
    for cmd in _candidate_pyinstaller_commands():
        try:
            proc = subprocess.run(cmd + ["--version"], capture_output=True, text=True, timeout=20)
            combined = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
            attempts.append(f"{' '.join(cmd)} -> rc={proc.returncode}{(' :: ' + combined) if combined else ''}")
            if proc.returncode == 0:
                return cmd, attempts
        except Exception as e:
            attempts.append(f"{' '.join(cmd)} -> {type(e).__name__}: {e}")
    raise RuntimeError(
        "PyInstaller was not found. Tried:\n- " + "\n- ".join(attempts) +
        "\nInstall it in the same environment as ncw with: python -m pip install pyinstaller"
    )


def build_exe_from_twin_target(target: str, base: str, search_paths: list[str]) -> str:
    if _is_url(target):
        raise RuntimeError("--exe currently supports local .nc files only.")

    src_path = os.path.abspath(target)
    if not os.path.isfile(src_path):
        raise FileNotFoundError(src_path)

    exe_name = _safe_exe_name_from_target(src_path)
    pyinstaller_cmd, _pyinstaller_attempts = _resolve_pyinstaller_command()

    build_root = Path(base if (base and not _is_url(base)) else os.path.dirname(src_path)).resolve()
    output_root = build_root / "nc_twin_exe_build"
    output_root.mkdir(parents=True, exist_ok=True)
    data_separator = ";" if os.name == "nt" else ":"

    safe_search_paths = _existing_search_paths(search_paths)
    module_name = Path(THIS_FILE).stem

    hidden_imports = [
        module_name,
        "nc_console",
        "nc",
        "t_windows",
        "nc_diagnostics",
        "nc_runtime_support",
        "nc_module_registry",
        "nc_ui_app",
        "nc_physics2d",
        "nc_physics2d_app",
        "nc_physics3d",
        "nc_physics3d_app",
        "panda3d.core",
        "direct.showbase.ShowBase",
        # stdlib imports that the frozen launcher / host / nc runtime need very early
        "argparse",
        "base64",
        "json",
        "tempfile",
        "shutil",
        "pathlib",
        "subprocess",
        "urllib.request",
        "urllib.parse",
        "decimal",
        "tokenize",
        "dataclasses",
        "typing",
        "ipaddress",
        "hmac",
        "hashlib",
        "secrets",
        "socket",
        "random",
        "math",
        "re",
        "io",
        "ast",
        "PySide6.QtWebEngineWidgets",
        "PySide6.QtWebChannel",
        "PySide6.QtMultimedia",
        "PySide6.QtMultimediaWidgets",
    ]

    launcher_code = f'''# Auto-generated by nc_twin_run.py --exe
from __future__ import annotations
# Preload stdlib modules BEFORE importing the GUI host.
# Bugfix: some frozen builds otherwise fail very early with missing stdlib
# imports like `json` when the host module is imported dynamically.
import argparse  # noqa: F401
import base64  # noqa: F401
import json  # noqa: F401
import os
import pathlib  # noqa: F401
import shutil  # noqa: F401
import subprocess  # noqa: F401
import sys
import tempfile  # noqa: F401
import urllib.parse  # noqa: F401
import urllib.request  # noqa: F401
import decimal  # noqa: F401
import tokenize  # noqa: F401
import dataclasses  # noqa: F401
import typing  # noqa: F401
import ipaddress  # noqa: F401
import hmac  # noqa: F401
import hashlib  # noqa: F401
import secrets  # noqa: F401
import socket  # noqa: F401
import random  # noqa: F401
import math  # noqa: F401
import re  # noqa: F401
import io  # noqa: F401
import ast  # noqa: F401

HERE = os.path.dirname(os.path.abspath(__file__))
PROGRAM_ROOT = os.path.join(HERE, "program")
SOURCE_PATH = os.path.join(PROGRAM_ROOT, {os.path.basename(src_path)!r})
EXTRA_PATHS = [PROGRAM_ROOT, os.path.join(PROGRAM_ROOT, "libs")]
for _p in list(EXTRA_PATHS):
    if _p and _p not in sys.path:
        sys.path.insert(0, _p)
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import {module_name} as twin

if __name__ == "__main__":
    _forward = sys.argv[1:] if len(sys.argv) > 1 else [SOURCE_PATH]
    raise SystemExit(twin.main(_forward))
'''

    with tempfile.TemporaryDirectory(prefix="nc_twin_exe_") as tmp:
        launcher = Path(tmp) / f"{exe_name}_twin_launcher.py"
        launcher.write_text(launcher_code, encoding="utf-8")
        cmd = pyinstaller_cmd + [
            "--noconfirm",
            "--clean",
            "--onedir",
            "--windowed",
            "--name", exe_name,
            "--distpath", str(output_root / "dist"),
            "--workpath", str(output_root / "build"),
            "--specpath", str(output_root / "spec"),
            "--add-data", f"{str(build_root)}{data_separator}program",
            "--add-data", f"{str(src_path)}{data_separator}program",
            "--collect-submodules", "PySide6",
            "--collect-submodules", "PySide6.QtWebEngineCore",
            "--collect-submodules", "PySide6.QtWebEngineWidgets",
            "--collect-submodules", "PySide6.QtWebChannel",
            "--collect-submodules", "PySide6.QtMultimedia",
            "--collect-submodules", "PySide6.QtMultimediaWidgets",
            "--collect-data", "PySide6",
        ]
        for hidden in hidden_imports:
            cmd.extend(["--hidden-import", hidden])
        cmd.append(str(launcher))
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            details = (proc.stderr or "").strip() or (proc.stdout or "").strip() or "PyInstaller failed"
            low = details.lower().replace("'", "")
            if "no module named pyinstaller" in low:
                tried = "\n- " + "\n- ".join(_pyinstaller_attempts) if _pyinstaller_attempts else ""
                raise RuntimeError(
                    "PyInstaller could not be started from the current ncw environment." + tried +
                    "\nInstall it in the same venv with: python -m pip install pyinstaller"
                ) from None
            raise RuntimeError(details)

    executable_suffix = ".exe" if os.name == "nt" else ""
    exe_path = output_root / "dist" / exe_name / f"{exe_name}{executable_suffix}"
    if not exe_path.is_file():
        raise RuntimeError(f"Build finished but EXE was not found: {exe_path}")
    return str(exe_path)



def _argparse_stream():
    stream = getattr(sys, "stderr", None)
    if stream is not None and hasattr(stream, "write"):
        return stream
    stream = getattr(sys, "stdout", None)
    if stream is not None and hasattr(stream, "write"):
        return stream

    class _NullStream:
        def write(self, _msg):
            return 0
        def flush(self):
            return None

    return _NullStream()


class _NCWArgumentParser(argparse.ArgumentParser):
    """
    Bugfix for windowed/frozen EXEs:
    sys.stderr can be None, which makes argparse crash with
    AttributeError: 'NoneType' object has no attribute 'write'.

    We also keep errors readable instead of hard-crashing the launcher.
    """
    def _print_message(self, message, file=None):
        if not message:
            return
        stream = file if (file is not None and hasattr(file, "write")) else _argparse_stream()
        try:
            stream.write(message)
        except Exception:
            pass

    def exit(self, status=0, message=None):
        if message:
            self._print_message(message)
        raise SystemExit(status)

    def error(self, message):
        self.print_usage(_argparse_stream())
        raise SystemExit(f"{self.prog}: error: {message}")


def _build_parser() -> argparse.ArgumentParser:
    parser = _NCWArgumentParser(
        prog="ncw",
        description="NC GUI Host / TWIN runner"
    )
    try:
        import nc as _nc_version_source

        parser.add_argument("--version", action="version", version=f"NCW {_nc_version_source.__version__}")
    except Exception:
        pass
    parser.add_argument("target", nargs="?", help="Path or URL to the .nc/.nce file")
    parser.add_argument("--base", dest="base", help="Base path or URL directory")
    parser.add_argument("--libs", dest="libs", action="append", default=[], help="Additional library search path (repeatable)")
    parser.add_argument("--exe", dest="exe", action="store_true", help="Build the target as a standalone executable")
    return parser


def _run_nc_child(argv: list[str]) -> int:
    child = list(argv or [])

    # A frozen PyInstaller executable is not a normal Python interpreter.
    # Starting ``sys.executable nc_console.py ...`` here would launch this
    # same GUI executable again, because PyInstaller dispatches the first
    # argument through this module's GUI entry point.  The old behaviour
    # therefore created an endless chain of NC Output windows in --exe builds.
    # Run the bundled console entry point in the child process instead.
    if getattr(sys, "frozen", False):
        try:
            from nc_console import main as console_main
            return int(console_main(child) or 0)
        except Exception as error:
            try:
                import nc as _nc
                print(_nc.format_exception(error), file=sys.stderr)
            except Exception:
                print(f"NC child error: {error}", file=sys.stderr)
            return 1

    cmd = [sys.executable, NC_CONSOLE] + child
    proc = subprocess.run(cmd)
    return int(proc.returncode or 0)

def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        import nc as _nc_selector_source
        version_selector = f"--{_nc_selector_source.__version__}"
        if argv and str(argv[0]).lower() == version_selector.lower():
            argv = argv[1:]
    except Exception:
        pass

    if "--__nc_child__" in argv:
        idx = argv.index("--__nc_child__")
        child_argv = argv[idx + 1:]
        return _run_nc_child(child_argv)

    parser = _build_parser()
    try:
        args, unknown = parser.parse_known_args(argv)
    except SystemExit as e:
        msg = str(e).strip()
        if msg and msg != "0":
            print(msg)
        return int(e.code) if isinstance(e.code, int) else 2

    if unknown:
        # Freeze-Regel bugfix: ignore foreign launcher/runtime flags instead of
        # crashing inside argparse in windowed builds.
        print("[ncw] ignored unknown args:", " ".join(map(str, unknown)))

    if not args.target:
        parser.print_help(_argparse_stream())
        return 2

    target = _resolve_local_target(args.target)
    if (not _is_url(target)) and (not target.lower().endswith((".nc", ".nce"))):
        if os.path.isfile(target + ".nc"):
            target = target + ".nc"
        elif os.path.isfile(target + ".nce"):
            target = target + ".nce"

    base = args.base or _compute_base(target)
    search_paths: list[str] = []
    if not _is_url(base):
        search_paths.extend([base, os.path.join(base, "libs")])
    search_paths.extend(list(args.libs or []))

    if args.exe:
        exe_path = build_exe_from_twin_target(target=target, base=base, search_paths=search_paths)
        print("NC TWIN EXE:", exe_path)
        return 0

    host = HostApp([sys.argv[0]] + argv, [target], target=target, base=base, search_paths=search_paths)
    return host.run()


if __name__ == "__main__":
    raise SystemExit(main())
