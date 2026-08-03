# =========================================================
# t_windows.py  (Sandbox-safe UI API for t_browser)
# - NO Qt imports here (no PySide6, no PyQt5) -> crash-safe
# - Emits commands via stdout lines: "__TWIN__" + base64(json)
# - t_browser must parse these commands and open real windows
# =========================================================

from __future__ import annotations
import json
import base64
import html as _html
import itertools
import random
import ast
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Union, Iterable, Tuple

_id_gen = itertools.count(1)

# ---------- low-level command emit ----------

def _b64_json(obj: dict) -> str:
    s = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    return base64.b64encode(s.encode("utf-8")).decode("ascii")

def _emit(cmd: dict) -> None:
    print("__TWIN__" + _b64_json(cmd))

def _esc(s: Any) -> str:
    return _html.escape("" if s is None else str(s), quote=True)

# ---------- cell helper objects ----------

class Html:
    """Mark raw HTML as trusted (inserted as-is)."""
    def __init__(self, html: str):
        self.html = "" if html is None else str(html)

class PyExpr:
    """
    Safe python expression (NOT full code).
    Only arithmetic + a few safe functions; no imports; no attributes.
    """
    def __init__(self, expr: str):
        self.expr = "" if expr is None else str(expr)

class Choice:
    """Randomly pick one option at render time."""
    def __init__(self, options: Iterable[Any]):
        self.options = list(options)

# ---------- tkinter-ish core ----------

@dataclass
class Style:
    windowBg: Optional[str] = None
    windowBorder: Optional[str] = None
    windowRadius: Optional[str] = None
    windowShadow: Optional[str] = None
    headerBg: Optional[str] = None
    headerColor: Optional[str] = None
    contentBg: Optional[str] = None

    def to_dict(self) -> dict:
        d = {}
        for k, v in self.__dict__.items():
            if v is not None:
                d[k] = v
        return d

@dataclass
class WindowConfig:
    title: str = "Window"
    icon: str = "T"
    x: int = -1
    y: int = -1
    w: int = 420
    h: int = 320
    fullscreen: bool = False
    style: Style = field(default_factory=Style)
    content_html: Optional[str] = None

class Tk:
    def __init__(self, app_title: str = "T-App"):
        self.app_title = app_title
        self.desktop_style: Dict[str, Any] = {}
        self.taskbar_style: Dict[str, Any] = {}
        self.cursor_style: Dict[str, Any] = {}
        self._windows: List[Toplevel] = []
        self._rng = random.Random()

    def set_desktop_style(self, **kwargs):
        self.desktop_style.update(kwargs)

    def set_taskbar_style(self, **kwargs):
        self.taskbar_style.update(kwargs)

    def set_cursor_style(self, **kwargs):
        self.cursor_style.update(kwargs)

    def set_random_seed(self, seed: Optional[int] = None):
        self._rng = random.Random(seed)

    def Toplevel(self, title: str = "Window", icon: str = "T") -> "Toplevel":
        w = Toplevel(root=self, title=title, icon=icon)
        self._windows.append(w)
        return w

    def mainloop(self):
        payload = {
            "action": "init",
            "app_title": self.app_title,
            "desktop_style": self.desktop_style,
            "taskbar_style": self.taskbar_style,
            "cursor_style": self.cursor_style,
            "windows": [w._to_payload() for w in self._windows],
        }
        _emit(payload)

    run = mainloop

class Toplevel:
    def __init__(self, root: Optional[Tk], title: str = "Window", icon: str = "T"):
        self.root = root
        self.cfg = WindowConfig(title=title, icon=icon)
        self._widgets: List[_Widget] = []
        self._wid = f"tw_{next(_id_gen)}"

    def geometry(self, geom: str) -> "Toplevel":
        try:
            parts = geom.split("+")
            wh = parts[0].lower().split("x")
            self.cfg.w = int(wh[0])
            self.cfg.h = int(wh[1])
            if len(parts) >= 3:
                self.cfg.x = int(parts[1])
                self.cfg.y = int(parts[2])
        except Exception:
            pass
        return self

    def position(self, x: int, y: int) -> "Toplevel":
        self.cfg.x = int(x)
        self.cfg.y = int(y)
        return self

    def size(self, w: int, h: int) -> "Toplevel":
        self.cfg.w = int(w)
        self.cfg.h = int(h)
        return self

    def fullscreen(self, on: bool = True) -> "Toplevel":
        self.cfg.fullscreen = bool(on)
        return self

    def style(self, **kwargs) -> "Toplevel":
        for k, v in kwargs.items():
            if hasattr(self.cfg.style, k):
                setattr(self.cfg.style, k, v)
        return self

    def set_html(self, html: str) -> "Toplevel":
        self.cfg.content_html = html
        return self

    def add(self, widget: "_Widget") -> "_Widget":
        self._widgets.append(widget)
        return widget

    def show(self) -> None:
        _emit({"action": "create", **self._to_payload()})

    def close(self) -> None:
        _emit({"action": "close", "id": self._wid})

    def _rng(self) -> random.Random:
        if self.root and hasattr(self.root, "_rng"):
            return self.root._rng
        return random.Random()

    def _to_payload(self) -> dict:
        if self.cfg.content_html is not None:
            content_html = self.cfg.content_html
        else:
            content_html = "<div class='tw_stack'>" + "".join(w.render(self._rng()) for w in self._widgets) + "</div>"

        return {
            "action": "create",
            "id": self._wid,
            "title": self.cfg.title,
            "icon": self.cfg.icon,
            "x": self.cfg.x,
            "y": self.cfg.y,
            "w": self.cfg.w,
            "h": self.cfg.h,
            "fullscreen": bool(self.cfg.fullscreen),
            "style": self.cfg.style.to_dict(),
            "content_html": content_html,
        }

# ---------- safe expression (no imports, no attributes, no statements) ----------

class _CalcError(Exception):
    pass

_ALLOWED_FUNCS = {
    "abs": abs,
    "round": round,
    "min": min,
    "max": max,
    "sum": sum,
}

def _safe_calc_expr(expr: str, rng: random.Random) -> Any:
    s = (expr or "").strip()
    if not s:
        return ""

    low = s.lower()
    # block obvious dangerous tokens (avoid putting patterns like "eval(" in this file!)
    bad_tokens = ["import", "__", "open", "exec", "compile", "globals", "locals", "subprocess", "socket"]
    for t in bad_tokens:
        if t in low:
            raise _CalcError("blocked token")

    # parse expression only
    node = ast.parse(s, mode="eval")

    def walk(n):
        if isinstance(n, ast.Expression):
            return walk(n.body)

        if isinstance(n, ast.Constant):
            return n.value

        if isinstance(n, ast.Num):  # older python
            return n.n

        if isinstance(n, ast.BinOp):
            a = walk(n.left)
            b = walk(n.right)
            if isinstance(n.op, ast.Add): return a + b
            if isinstance(n.op, ast.Sub): return a - b
            if isinstance(n.op, ast.Mult): return a * b
            if isinstance(n.op, ast.Div): return a / b
            if isinstance(n.op, ast.FloorDiv): return a // b
            if isinstance(n.op, ast.Mod): return a % b
            raise _CalcError("op not allowed")

        if isinstance(n, ast.UnaryOp):
            v = walk(n.operand)
            if isinstance(n.op, ast.UAdd): return +v
            if isinstance(n.op, ast.USub): return -v
            raise _CalcError("unary not allowed")

        if isinstance(n, ast.Name):
            if n.id == "pi":
                return 3.141592653589793
            if n.id == "e":
                return 2.718281828459045
            if n.id in _ALLOWED_FUNCS:
                return _ALLOWED_FUNCS[n.id]
            # random helpers
            if n.id == "rand":
                return lambda: rng.random()
            if n.id == "randint":
                return lambda a, b: rng.randint(int(a), int(b))
            if n.id == "choice":
                return lambda seq: rng.choice(list(seq))
            raise _CalcError("name not allowed")

        if isinstance(n, ast.Call):
            f = walk(n.func)
            if not callable(f):
                raise _CalcError("call not allowed")
            args = [walk(a) for a in n.args]
            # no kwargs
            if getattr(n, "keywords", None):
                if len(n.keywords) > 0:
                    raise _CalcError("kwargs not allowed")
            return f(*args)

        # block everything else: Attribute, Subscript, Lambda, Comprehensions, etc.
        raise _CalcError("node not allowed")

    return walk(node)

# ---------- widgets ----------

class _Widget:
    def render(self, rng: random.Random) -> str:
        raise NotImplementedError

class Frame(_Widget):
    def __init__(self, parent: Toplevel, title: str = ""):
        self.title = title
        self.children: List[_Widget] = []
        parent.add(self)

    def add(self, w: _Widget) -> _Widget:
        self.children.append(w)
        return w

    def render(self, rng: random.Random) -> str:
        inner = "".join(c.render(rng) for c in self.children)
        title = f"<div class='tw_frame_title'>{_esc(self.title)}</div>" if self.title else ""
        return f"<div class='tw_frame'>{title}{inner}</div>"

class Label(_Widget):
    def __init__(self, parent: Union[Toplevel, Frame], text: str, font_size: int = 16, bold: bool = False):
        self.text = text
        self.font_size = int(font_size)
        self.bold = bool(bold)
        parent.add(self) if isinstance(parent, Frame) else parent.add(self)

    def render(self, rng: random.Random) -> str:
        fw = "700" if self.bold else "500"
        return (
            f"<div class='tw_label' style='margin:10px 10px;"
            f"font-size:{self.font_size}px;font-weight:{fw};'>{_esc(self.text)}</div>"
        )

class Entry(_Widget):
    def __init__(self, parent: Union[Toplevel, Frame], placeholder: str = "", value: str = ""):
        self.placeholder = placeholder
        self.value = value
        parent.add(self) if isinstance(parent, Frame) else parent.add(self)

    def render(self, rng: random.Random) -> str:
        return (
            f"<input class='tw_entry' style='margin:8px 10px;width:100%;' "
            f"placeholder='{_esc(self.placeholder)}' value='{_esc(self.value)}' />"
        )

class Text(_Widget):
    def __init__(self, parent: Union[Toplevel, Frame], value: str = "", rows: int = 8):
        self.value = value
        self.rows = int(rows)
        parent.add(self) if isinstance(parent, Frame) else parent.add(self)

    def render(self, rng: random.Random) -> str:
        return (
            f"<textarea class='tw_text' style='margin:8px 10px;width:100%;' rows='{self.rows}'>"
            f"{_esc(self.value)}</textarea>"
        )

class Button(_Widget):
    def __init__(self, parent: Union[Toplevel, Frame], text: str = "Button",
                 command: Optional[Union[str, Callable[[], Any]]] = None):
        self.text = text
        self.command = command
        self._btn_id = f"btn_{next(_id_gen)}"
        parent.add(self) if isinstance(parent, Frame) else parent.add(self)

    def render(self, rng: random.Random) -> str:
        if callable(self.command):
            onclick = f"window.TWIN && window.TWIN.emit && window.TWIN.emit('py_callback','{_esc(self._btn_id)}');"
        elif isinstance(self.command, str) and self.command.strip():
            onclick = self.command
        else:
            onclick = ""
        return (
            f"<button class='tw_btn' style='margin:6px 10px;' "
            f"onclick=\"{_esc(onclick)}\">{_esc(self.text)}</button>"
        )

class Table(_Widget):
    def __init__(self, parent: Union[Toplevel, Frame],
                 headers: Optional[List[Any]] = None,
                 rows: Optional[List[List[Any]]] = None,
                 striped: bool = True):
        self.headers = headers or []
        self.rows = rows or []
        self.striped = bool(striped)
        parent.add(self) if isinstance(parent, Frame) else parent.add(self)

    def add_row(self, *cells: Any):
        self.rows.append(list(cells))
        return self

    def _render_cell(self, cell: Any, rng: random.Random) -> str:
        # random choice
        if isinstance(cell, Choice):
            if not cell.options:
                return ""
            cell = rng.choice(cell.options)

        # python expression
        if isinstance(cell, PyExpr):
            try:
                v = _safe_calc_expr(cell.expr, rng)
                return _esc(v)
            except Exception:
                return "<span style='opacity:.75;color:#ffb3b3;'>[py-error]</span>"

        # raw html
        if isinstance(cell, Html):
            return cell.html

        # normal
        return _esc(cell)

    def render(self, rng: random.Random) -> str:
        css = """
        <style>
          .tw_table_wrap{ margin:10px; overflow:auto; border-radius:14px;
            border:1px solid rgba(255,255,255,0.18); background: rgba(255,255,255,0.06); }
          table.tw_table{ width:100%; border-collapse: collapse; }
          .tw_table th, .tw_table td{
            padding:10px 12px; border-bottom:1px solid rgba(255,255,255,0.12);
            vertical-align: top;
          }
          .tw_table th{ text-align:left; font-weight:900; opacity:0.95;
            background: rgba(79,209,255,0.10); border-bottom:1px solid rgba(79,209,255,0.22); }
          .tw_table tr:nth-child(even) td{ background: rgba(255,255,255,0.03); }
          .tw_mono{ font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
        </style>
        """

        thead = ""
        if self.headers:
            hs = "".join(f"<th>{self._render_cell(h, rng)}</th>" for h in self.headers)
            thead = f"<thead><tr>{hs}</tr></thead>"

        body_rows = []
        for r in self.rows:
            tds = "".join(f"<td>{self._render_cell(c, rng)}</td>" for c in r)
            body_rows.append(f"<tr>{tds}</tr>")
        tbody = "<tbody>" + "".join(body_rows) + "</tbody>"

        return css + f"<div class='tw_table_wrap'><table class='tw_table'>{thead}{tbody}</table></div>"

# ---------- convenience API ----------

def create_window(title: str = "Window", html: str = "<p>Empty</p>", w: int = 900, h: int = 600,
                  x: int = -1, y: int = -1, icon: str = "T", style: Optional[dict] = None,
                  fullscreen: bool = False):
    _emit({
        "action": "create",
        "id": f"tw_{next(_id_gen)}",
        "title": title,
        "icon": icon,
        "x": int(x),
        "y": int(y),
        "w": int(w),
        "h": int(h),
        "fullscreen": bool(fullscreen),
        "style": style or {},
        "content_html": html,
    })

def close_window(window_id: str):
    _emit({"action": "close", "id": str(window_id)})

# ---------- messagebox helpers ----------

def messagebox_info(title: str, message: str):
    _emit({"action": "msgbox", "kind": "info", "title": title, "message": message})

def messagebox_warning(title: str, message: str):
    _emit({"action": "msgbox", "kind": "warning", "title": title, "message": message})

def messagebox_error(title: str, message: str):
    _emit({"action": "msgbox", "kind": "error", "title": title, "message": message})

def messagebox_askyesno(title: str, message: str, default: bool = False):
    _emit({"action": "msgbox", "kind": "askyesno", "title": title, "message": message, "default": bool(default)})

# ---------- small JS helpers ----------

def js_alert(msg: str) -> str:
    return f"alert({json.dumps(str(msg))});"

def js_console(msg: str) -> str:
    return f"console.log({json.dumps(str(msg))});"

def js_open_url(url: str, target: str = "_blank") -> str:
    return f"window.open({json.dumps(str(url))}, {json.dumps(str(target))});"
