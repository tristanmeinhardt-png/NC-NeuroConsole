# nc.py — NeuroConsole (NC) v1.2.0-alpha.2 core
# ============================================================
# Includes:
#  Parser keeps indentation after stripping comments (your new parser logic)
#  Errors ALWAYS include: file/source + line number (for ALL error types)
#  Parser collects MULTIPLE errors per file and prints them all
#  Main file parse errors show the REAL file path (fixed via source_name plumbing)
#  Runtime / expression / import errors are wrapped with file+line context
#  Optional import-stack context in error messages (helps find where it came from)
#
# PATCH (Dec 2025):
#  Defensive: NCReturn ("ret"/"return") should never escape to top-level.
#  If it leaks, we store __last_return__ and CONTINUE instead of crashing,
#  so modules like error_view_plus can still show UI + tables.
# ============================================================

from __future__ import annotations

import os
import re
import ast
import json
import hashlib
import hmac
import secrets

import time
import random
import socket
import urllib.request
import ipaddress
import math as _pymath
import decimal
import tokenize
import io
import sys
import difflib
from dataclasses import dataclass, field
from typing import Any, Dict, List, Tuple, Optional, Callable
from urllib.parse import urlparse, urljoin

import nc_diagnostics as _diagnostics


__version__ = "1.2.0-alpha.2"


# -----------------------------
# Your requested standard imports folder
# -----------------------------
def _resolve_standard_imports_dir() -> str:
    """
    Resolve the NC standard imports directory in a portable, bugfix-safe way.

    Priority:
    1) NC_STANDARD_IMPORTS_DIR env var
    2) "standart_imports" next to this nc.py file
    3) ~/NC/standart_imports

    We also create the preferred directory when possible, so first-run imports
    do not fail just because the folder has not been created yet.
    """
    candidates: List[str] = []

    env_dir = os.environ.get("NC_STANDARD_IMPORTS_DIR", "").strip()
    if env_dir:
        candidates.append(os.path.abspath(os.path.expanduser(env_dir)))

    local_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "standart_imports")
    home_dir = os.path.join(os.path.expanduser("~"), "NC", "standart_imports")

    candidates.extend([local_dir, home_dir])

    for cand in candidates:
        try:
            if cand and os.path.isdir(cand):
                return cand
        except Exception:
            pass

    preferred = local_dir if os.path.isdir(os.path.dirname(local_dir)) else home_dir
    try:
        os.makedirs(preferred, exist_ok=True)
    except Exception:
        pass

    return preferred


STANDARD_IMPORTS_DIR = _resolve_standard_imports_dir()


# -----------------------------
# Policy knobs
# -----------------------------
class NCPolicy:
    def __init__(
        self,
        allow_http: bool = False,
        allow_private_hosts: bool = False,
        max_module_bytes: int = 300_000,
        url_timeout_sec: int = 8,
        max_import_depth: int = 40,
        max_steps: int = 2_000_000,  # global execution step budget
        max_expr_len: int = 5_000,
        data_dir: Optional[str] = None,  # for json.save/load
    ):
        self.allow_http = allow_http
        self.allow_private_hosts = allow_private_hosts
        self.max_module_bytes = max_module_bytes
        self.url_timeout_sec = url_timeout_sec
        self.max_import_depth = max_import_depth
        self.max_steps = max_steps
        self.max_expr_len = max_expr_len
        self.data_dir = data_dir  # if None => ./data


# -----------------------------
# Builtin NC modules (text fallback)
# -----------------------------
BUILTIN_NC_MODULES: Dict[str, str] = {
    "ui": r"""
# builtin ui.nc (legacy placeholder)
export window
export plot
export table
export tick
""".strip(),
    "math": r"""
# builtin math.nc (legacy placeholder)
export clamp
export clip
export mean
export std
export sigmoid
export softmax
""".strip(),
    "json": r"""
# builtin json.nc (legacy placeholder)
export encode
export pretty
export parse
export diff
export to_table
export schema_validate
export save
export load
""".strip(),
}


# ============================================================
# Error types / helpers
# ============================================================

@dataclass
class NCReportedError:
    source: str
    line: int
    message: str
    column: int = 1
    code: Optional[str] = None
    help: Optional[str] = None

    def format(self) -> str:
        return _diagnostics.format_errors([self])


class NCMultiError(Exception):
    def __init__(self, errors: List[NCReportedError], header: str = "NC errors"):
        self.errors = errors
        super().__init__(header)

    def format(self) -> str:
        return _diagnostics.format_errors(self.errors, str(self))


class NCError(Exception):
    """
    Single error that keeps source/line and optionally import stack context.
    """

    def __init__(
        self,
        source: str,
        line: int,
        message: str,
        import_stack: Optional[List[str]] = None,
        *,
        column: int = 1,
        code: Optional[str] = None,
        help: Optional[str] = None,
    ):
        self.source = source
        self.line = int(line)
        self.message = str(message)
        self.import_stack = list(import_stack) if import_stack else []
        self.column = max(1, int(column))
        self.code = code
        self.help = help
        self.call_stack: List[Dict[str, Any]] = []
        super().__init__(self.__str__())

    def add_frame(self, name: str, source: str, line: int) -> "NCError":
        frame = {"name": str(name), "source": str(source), "line": int(line)}
        if not self.call_stack or self.call_stack[-1] != frame:
            self.call_stack.append(frame)
        return self

    def __str__(self) -> str:
        return _diagnostics.format_exception(self)


def format_exception(error: BaseException) -> str:
    """Public formatter used by nc, ncw, servers, and embedding hosts."""

    return _diagnostics.format_exception(error)


def _format_source(name: str) -> str:
    return str(name or "<text>")



def _nc_try_number(value: Any) -> Any:
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, decimal.Decimal):
        try:
            return float(value)
        except Exception:
            return value
    if isinstance(value, str):
        s = value.strip().replace(",", ".")
        if re.fullmatch(r"-?\d+(?:\.\d+)?", s):
            try:
                return float(s) if "." in s else int(s)
            except Exception:
                return value
    return value

def _nc_to_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, (int, float, decimal.Decimal)):
        return value != 0
    if isinstance(value, str):
        s = value.strip().lower()
        if s in ("", "0", "false", "off", "no", "none", "null"):
            return False
        if s in ("1", "true", "on", "yes"):
            return True
    return bool(value)

# ============================================================
# Utilities
# ============================================================

def _is_url(s: str) -> bool:
    return s.startswith("https://") or s.startswith("http://")


def _blocked_scheme(url: str, policy: NCPolicy) -> bool:
    if url.startswith("https://"):
        return False
    if url.startswith("http://"):
        return not policy.allow_http
    return True


def _is_private_or_localhost(host: str) -> bool:
    if not host:
        return True
    if host.lower() in ("localhost",):
        return True
    try:
        ip = ipaddress.ip_address(socket.gethostbyname(host))
        return ip.is_private or ip.is_loopback or ip.is_link_local
    except Exception:
        return False


def _fetch_url_text(url: str, policy: NCPolicy) -> str:
    if _blocked_scheme(url, policy):
        raise ValueError(f"Blocked URL scheme (https only by default): {url}")

    host = urlparse(url).hostname or ""
    if (not policy.allow_private_hosts) and _is_private_or_localhost(host):
        raise ValueError(f"Blocked private/localhost import: {url}")

    req = urllib.request.Request(url, headers={"User-Agent": "NC/1.0"})
    with urllib.request.urlopen(req, timeout=policy.url_timeout_sec) as r:
        data = r.read(policy.max_module_bytes + 1)
    if len(data) > policy.max_module_bytes:
        raise ValueError(f"Blocked: module too large: {url}")
    return data.decode("utf-8", errors="replace")


def _read_file_text(path: str, policy: NCPolicy) -> str:
    if not os.path.isfile(path):
        raise FileNotFoundError(path)
    if os.path.getsize(path) > policy.max_module_bytes:
        raise ValueError(f"Blocked: module too large: {path}")
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return f.read()


def _resolve_ref(base: str, name_or_path: str) -> str:
    # base: folder path or URL directory
    if _is_url(base):
        b = base.rstrip("/") + "/"
        if _is_url(name_or_path):
            return name_or_path
        if name_or_path.endswith(".nc"):
            return urljoin(b, name_or_path)
        return urljoin(b, name_or_path + ".nc")
    else:
        b = os.path.abspath(base)
        if name_or_path.endswith(".nc"):
            return os.path.join(b, name_or_path)
        return os.path.join(b, name_or_path + ".nc")


def _split_commas(s: str) -> List[str]:
    # split comma-separated values safely while respecting quotes and nesting.
    # supports (), [] and {} so calls like:
    #   startswith("T-OS", "T-")
    # are not split incorrectly.
    out: List[str] = []
    buf: List[str] = []
    q: Optional[str] = None
    depth_paren = 0
    depth_brack = 0
    depth_brace = 0
    esc = False

    for ch in str(s):
        if q:
            buf.append(ch)
            if esc:
                esc = False
                continue
            if ch == "\\":
                esc = True
                continue
            if ch == q:
                q = None
            continue

        if ch in ("'", '"'):
            q = ch
            buf.append(ch)
            continue

        if ch == '(':
            depth_paren += 1
            buf.append(ch)
            continue
        if ch == ')':
            depth_paren = max(0, depth_paren - 1)
            buf.append(ch)
            continue
        if ch == '[':
            depth_brack += 1
            buf.append(ch)
            continue
        if ch == ']':
            depth_brack = max(0, depth_brack - 1)
            buf.append(ch)
            continue
        if ch == '{':
            depth_brace += 1
            buf.append(ch)
            continue
        if ch == '}':
            depth_brace = max(0, depth_brace - 1)
            buf.append(ch)
            continue

        if ch == ',' and depth_paren == 0 and depth_brack == 0 and depth_brace == 0:
            part = ''.join(buf).strip()
            if part:
                out.append(part)
            buf = []
            continue

        buf.append(ch)

    part = ''.join(buf).strip()
    if part:
        out.append(part)
    return out


_INLINE_COMMAND_KEYWORDS = (
    "button", "botton", "knopf", "checkmark", "checkbox", "check",
    "haken", "haekchen", "häckchen", "print", "input",
)


def _split_inline_commands(s: str) -> List[Tuple[str, str]]:
    """Split an explicit horizontal NC control/output row.

    A command boundary is recognized only outside strings and nested
    expressions. This keeps values such as ``print "button A"`` intact while
    allowing rows like ``button "A" button "B"``.
    """
    text = str(s or "").strip()
    if not text:
        return []

    positions: List[Tuple[int, int, str]] = []  # (command start, keyword start, keyword)
    quote: Optional[str] = None
    escaped = False
    depth_paren = depth_brack = depth_brace = 0
    i = 0
    while i < len(text):
        ch = text[i]
        if quote:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == quote:
                quote = None
            i += 1
            continue

        if ch in ("'", '"'):
            quote = ch
            i += 1
            continue
        if ch == "(":
            depth_paren += 1
        elif ch == ")":
            depth_paren = max(0, depth_paren - 1)
        elif ch == "[":
            depth_brack += 1
        elif ch == "]":
            depth_brack = max(0, depth_brack - 1)
        elif ch == "{":
            depth_brace += 1
        elif ch == "}":
            depth_brace = max(0, depth_brace - 1)

        if depth_paren == 0 and depth_brack == 0 and depth_brace == 0:
            if i == 0 or text[i - 1].isspace():
                for keyword in _INLINE_COMMAND_KEYWORDS:
                    end = i + len(keyword)
                    if text.startswith(keyword, i) and (end == len(text) or not _is_ident_char(text[end])):
                        command_start = i
                        if keyword in {"checkmark", "checkbox", "check", "haken", "haekchen", "häckchen", "input"}:
                            prefix = re.search(r"(?:^|\s)(\([A-Za-z_]\w*\)\s*=\s*)$", text[:i])
                            if prefix:
                                command_start = prefix.start(1)
                        positions.append((command_start, i, keyword))
                        break
        i += 1

    if not positions or positions[0][0] != 0:
        return []

    rows: List[Tuple[str, str]] = []
    for index, (start, keyword_start, keyword) in enumerate(positions):
        end = positions[index + 1][0] if index + 1 < len(positions) else len(text)
        rows.append((keyword, text[start:end].strip()))
    return rows


# ============================================================
# Safe expression evaluator (AST whitelist)
# ============================================================

class NCExprError(Exception):
    pass


_ALLOWED_BINOPS = (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Mod, ast.Pow)
_ALLOWED_UNOPS = (ast.UAdd, ast.USub, ast.Not)
_ALLOWED_BOOLOPS = (ast.And, ast.Or)
_ALLOWED_CMPOPS = (
    ast.Eq, ast.NotEq,
    ast.Lt, ast.LtE, ast.Gt, ast.GtE,
    ast.In, ast.NotIn,
    ast.Is, ast.IsNot,
)

# IMPORTANT SECURITY FIX:
# Never include "ast.AST" as a fallback; it would whitelist everything.
_ALLOWED_NODES_BASE = (
    ast.Expression,
    ast.Constant,
    ast.Name,
    ast.Load,
    ast.BinOp,
    ast.UnaryOp,
    ast.BoolOp,
    ast.Compare,
    ast.IfExp,
    ast.Call,
    ast.List,
    ast.Tuple,
    ast.Dict,
    ast.Subscript,
    ast.Slice,
    ast.Attribute,
    ast.operator,
    ast.unaryop,
    ast.boolop,
    ast.cmpop,
)
if hasattr(ast, "Index"):
    _ALLOWED_NODES = _ALLOWED_NODES_BASE + (ast.Index,)  # type: ignore[attr-defined]
else:
    _ALLOWED_NODES = _ALLOWED_NODES_BASE


def _safe_name(name: str) -> bool:
    return re.fullmatch(r"[A-Za-z_]\w*", name) is not None


def _get_attr(obj: Any, attr: str) -> Any:
    if attr.startswith("_"):
        raise NCExprError("Blocked attribute access")
    if isinstance(obj, NCModule):
        return obj.get(attr)
    return getattr(obj, attr)


def _rewrite_implicit_call_expr(expr: str) -> str:
    """Allow simple NC call syntax without parentheses, e.g. input "Name?"."""
    s = str(expr or "").strip()
    if not s:
        return s
    # already normal python-like call
    if re.match(r'^[A-Za-z_]\w*\s*\(', s):
        return s
    # simple one-arg call: name <rest>
    m = re.match(r'^([A-Za-z_]\w*)\s+(.+)$', s)
    if not m:
        return s
    fn, arg = m.group(1), m.group(2).strip()
    if not arg:
        return s
    # avoid rewriting operators/comparisons or chained statements
    blocked = (" and ", " or ", " not in ", " is not ", " is ", " in ", "==", "!=", ">=", "<=", ">", "<")
    if any(tok in s for tok in blocked):
        return s
    return f"{fn}({arg})"


def safe_eval_expr(expr: str, env: Dict[str, Any], policy: NCPolicy) -> Any:
    # Checkmark comparison aliases are normalized before AST parsing. This was
    # historically implemented by redefining safe_eval_expr near the end of
    # the file; keeping it here makes the evaluator single-source again.
    expr = expr.replace(" is on", " == True")
    expr = expr.replace(" is off", " == False")
    if len(expr) > policy.max_expr_len:
        raise NCExprError("Expression too long")
    parse_expr = expr
    try:
        node = ast.parse(parse_expr, mode="eval")
    except Exception as e:
        alt = _rewrite_implicit_call_expr(expr)
        if alt != expr:
            try:
                node = ast.parse(alt, mode="eval")
                parse_expr = alt
            except Exception:
                raise NCExprError(f"Bad expression: {e}")
        else:
            raise NCExprError(f"Bad expression: {e}")
    except Exception as e:
        raise NCExprError(f"Bad expression: {e}")

    def check(n: ast.AST):
        if not isinstance(n, _ALLOWED_NODES):
            raise NCExprError(f"Blocked syntax: {type(n).__name__}")

        # Extra safety: block dict-unpack like {**x}
        if isinstance(n, ast.Dict):
            if any(k is None for k in n.keys):
                raise NCExprError("Dict unpack blocked")

        for child in ast.iter_child_nodes(n):
            check(child)

        if isinstance(n, ast.Name):
            if not _safe_name(n.id):
                raise NCExprError("Bad name")
        if isinstance(n, ast.Attribute):
            if n.attr.startswith("_"):
                raise NCExprError("Blocked attribute")
        if isinstance(n, ast.Call):
            if any(isinstance(a, ast.Starred) for a in n.args):
                raise NCExprError("Star args blocked")
            if n.keywords:
                raise NCExprError("Keyword args blocked")
        if isinstance(n, ast.BinOp):
            if not isinstance(n.op, _ALLOWED_BINOPS):
                raise NCExprError("BinOp blocked")
        if isinstance(n, ast.UnaryOp):
            if not isinstance(n.op, _ALLOWED_UNOPS):
                raise NCExprError("UnaryOp blocked")
        if isinstance(n, ast.BoolOp):
            if not isinstance(n.op, _ALLOWED_BOOLOPS):
                raise NCExprError("BoolOp blocked")
        if isinstance(n, ast.Compare):
            if not all(isinstance(op, _ALLOWED_CMPOPS) for op in n.ops):
                raise NCExprError("Compare op blocked")

    check(node)

    def ev(n: ast.AST):
        if isinstance(n, ast.Expression):
            return ev(n.body)

        if isinstance(n, ast.Constant):
            return n.value

        if isinstance(n, ast.Name):
            if n.id in env:
                return env[n.id]
            raise NCExprError(f"Unknown name: {n.id}")

        if isinstance(n, ast.Attribute):
            base = ev(n.value)
            if base is None:
                return None  # lenient: None.<attr> => None
            return _get_attr(base, n.attr)

        if isinstance(n, ast.List):
            return [ev(x) for x in n.elts]

        if isinstance(n, ast.Tuple):
            return tuple(ev(x) for x in n.elts)

        if isinstance(n, ast.Dict):
            # dict unpack already blocked in check()
            return {ev(k): ev(v) for k, v in zip(n.keys, n.values)}

        if isinstance(n, ast.Subscript):
            base = ev(n.value)
            if base is None:
                return None  # lenient: None[...] => None
            sl = n.slice
            if isinstance(sl, ast.Slice):
                lo = ev(sl.lower) if sl.lower else None
                hi = ev(sl.upper) if sl.upper else None
                st = ev(sl.step) if sl.step else None
                return base[slice(lo, hi, st)]
            else:
                idx = ev(sl)
                return base[idx]

        if isinstance(n, ast.UnaryOp):
            v = ev(n.operand)
            if isinstance(n.op, ast.UAdd):
                return +v
            if isinstance(n.op, ast.USub):
                return -v
            if isinstance(n.op, ast.Not):
                return (not _nc_to_bool(v))
            raise NCExprError("Unary op blocked")

        if isinstance(n, ast.BinOp):
            a, b = ev(n.left), ev(n.right)
            if isinstance(n.op, ast.Add):
                # lenient: allow string concatenation with mixed values
                if isinstance(a, str) or isinstance(b, str):
                    return ("" if a is None else str(a)) + ("" if b is None else str(b))
                # lenient: allow None + number / number + None
                if a is None and isinstance(b, (int, float)) and not isinstance(b, bool):
                    return 0 + b
                if b is None and isinstance(a, (int, float)) and not isinstance(a, bool):
                    return a + 0
                return a + b
            if isinstance(n.op, ast.Sub):
                return a - b
            if isinstance(n.op, ast.Mult):
                return a * b
            if isinstance(n.op, ast.Div):
                return a / b
            if isinstance(n.op, ast.Mod):
                return a % b
            if isinstance(n.op, ast.Pow):
                return a ** b
            raise NCExprError("Bin op blocked")

        if isinstance(n, ast.BoolOp):
            if isinstance(n.op, ast.And):
                for x in n.values:
                    if not _nc_to_bool(ev(x)):
                        return False
                return True
            if isinstance(n.op, ast.Or):
                for x in n.values:
                    if _nc_to_bool(ev(x)):
                        return True
                return False
            raise NCExprError("Bool op blocked")

        # ✅ FIXED INDENTATION + LOGIC
        if isinstance(n, ast.Compare):
            left = ev(n.left)
            for op, comp in zip(n.ops, n.comparators):
                right = ev(comp)

                # lenient: comparisons with None:
                # - ==, !=, is, is not behave normally
                # - order/in checks with None => False (no crash)
                if (left is None or right is None) and not isinstance(op, (ast.Eq, ast.NotEq, ast.Is, ast.IsNot)):
                    ok = False
                else:
                    try:
                        if isinstance(op, ast.Eq):
                            ok = (left == right)
                        elif isinstance(op, ast.NotEq):
                            ok = (left != right)
                        elif isinstance(op, ast.Lt):
                            ok = (left < right)
                        elif isinstance(op, ast.LtE):
                            ok = (left <= right)
                        elif isinstance(op, ast.Gt):
                            ok = (left > right)
                        elif isinstance(op, ast.GtE):
                            ok = (left >= right)
                        elif isinstance(op, ast.In):
                            ok = (left in right)
                        elif isinstance(op, ast.NotIn):
                            ok = (left not in right)
                        elif isinstance(op, ast.Is):
                            ok = (left is right)
                        elif isinstance(op, ast.IsNot):
                            ok = (left is not right)
                        else:
                            raise NCExprError("Compare op blocked")
                    except TypeError:
                        # lenient: incompatible types => False instead of crash
                        ok = False

                if not ok:
                    return False
                left = right

            return True

        if isinstance(n, ast.IfExp):
            return ev(n.body) if _nc_to_bool(ev(n.test)) else ev(n.orelse)

        if isinstance(n, ast.Call):
            fn = ev(n.func)
            args = [ev(a) for a in n.args]
            if isinstance(fn, NCFn):
                return fn.call(args, runtime_env=env)
            if callable(fn) and getattr(fn, "__nc_callable__", False):
                return fn(*args)
            return fn(*args) if callable(fn) else None  # lenient: non-callable call => None

        raise NCExprError(f"Unsupported node: {type(n).__name__}")

    return ev(node)

# ============================================================
# MathNC — Math-only sub-language (Decimal exact mode)
# Trigger:
#   First non-empty line is:  mathlang
# or file starts with:        #!math
#
# Only allowed top-level statements:
#   import <name>       (NC import, for modules)
#   export <name>       (NC export)
#   let <name> = <expr>
#   set <name> = <expr>
#   fn <name>(a,b,...):   (math-only body)
#     <expr>            (evaluated, last result auto-returned if no ret)
#     ret <expr>
#
# Any other line is treated as a math expression, evaluated, and printed.
#
# Extra operator from "Rechenart Script verbessern":
#   a Script b   == script(a,b) == (a + b) / 2
#   (left associative if chained)
# ============================================================

_MATH_TRIGGER_LINES = ("mathlang",)
_MATH_TRIGGER_PREFIXES = ("#!math",)

def _math_is_triggered(text: str) -> bool:
    for raw in (text or "").splitlines():
        s = (raw.split("#", 1)[0]).strip()
        if not s:
            continue
        low = s.lower()
        if low in _MATH_TRIGGER_LINES:
            return True
        for p in _MATH_TRIGGER_PREFIXES:
            if low.startswith(p):
                return True
        return False
    return False


def _math_split_by_keyword(expr: str, keyword: str) -> List[str]:
    """Split by keyword at top-level (paren depth 0). No strings allowed here."""
    out: List[str] = []
    buf: List[str] = []
    i = 0
    n = len(expr)
    depth = 0
    kw = keyword
    kwlen = len(kw)

    def flush():
        part = "".join(buf).strip()
        out.append(part)
        buf.clear()

    while i < n:
        ch = expr[i]
        if ch == "(":
            depth += 1
            buf.append(ch)
            i += 1
            continue
        if ch == ")":
            depth = max(0, depth - 1)
            buf.append(ch)
            i += 1
            continue

        if depth == 0:
            # match whole-word keyword
            if expr[i:i+kwlen].lower() == kw.lower():
                before = expr[i-1] if i > 0 else " "
                after = expr[i+kwlen] if i + kwlen < n else " "
                if (not before.isalnum()) and (before != "_") and (not after.isalnum()) and (after != "_"):
                    flush()
                    i += kwlen
                    continue

        buf.append(ch)
        i += 1

    flush()
    # remove possible empty tails
    return [p for p in out if p is not None]


def _math_apply_script_operator(expr: str) -> str:
    parts = _math_split_by_keyword(expr, "Script")
    if len(parts) <= 1:
        return expr
    # left associative: Script(Script(a,b),c)
    acc = parts[0]
    for p in parts[1:]:
        acc = f"script(({acc}),({p}))"
    return acc


def _math_fix_decimal_commas(expr: str) -> str:
    # German decimal comma -> dot, only between digits (no strings in MathNC)
    return re.sub(r"(?<=\d),(?=\d)", ".", expr)


def _math_decimalize_numbers(expr: str) -> str:
    """
    Convert NUMBER tokens into D("...") calls, preserving the original token text.
    This avoids float binary rounding (fixes 6.999999999999... issues).
    """
    expr = _math_fix_decimal_commas(expr)
    try:
        toks = list(tokenize.generate_tokens(io.StringIO(expr).readline))
    except Exception:
        return expr

    out: List[str] = []
    for t in toks:
        if t.type == tokenize.NUMBER:
            # Keep exact text
            out.append(f'D("{t.string}")')
        else:
            out.append(t.string)
    return "".join(out)


def _math_dec(x: Any) -> "decimal.Decimal":
    if isinstance(x, decimal.Decimal):
        return x
    if isinstance(x, bool):
        return decimal.Decimal(int(x))
    if isinstance(x, int):
        return decimal.Decimal(x)
    if isinstance(x, float):
        # best-effort: keep repr
        return decimal.Decimal(repr(x))
    if x is None:
        return decimal.Decimal(0)
    return decimal.Decimal(str(x).strip())


def _math_wrap_unary(fn):
    def _w(a):
        a = _math_dec(a)
        return _math_dec(fn(float(a)))
    _w.__nc_callable__ = True
    return _w

def _math_wrap_binary(fn):
    def _w(a, b):
        a = _math_dec(a); b = _math_dec(b)
        return _math_dec(fn(float(a), float(b)))
    _w.__nc_callable__ = True
    return _w

def _math_sqrt(a):
    a = _math_dec(a)
    try:
        return a.sqrt()
    except Exception:
        # fallback
        return _math_dec(_pymath.sqrt(float(a)))
_math_sqrt.__nc_callable__ = True

def _math_fact(a):
    a = int(_math_dec(a))
    if a < 0:
        raise ValueError("factorial needs n>=0")
    return decimal.Decimal(_pymath.factorial(a))
_math_fact.__nc_callable__ = True

def _math_gcd(a, b):
    return decimal.Decimal(_pymath.gcd(int(_math_dec(a)), int(_math_dec(b))))
_math_gcd.__nc_callable__ = True

def _math_lcm(a, b):
    aa = int(_math_dec(a)); bb = int(_math_dec(b))
    if aa == 0 or bb == 0:
        return decimal.Decimal(0)
    return decimal.Decimal(abs(aa * bb) // _pymath.gcd(aa, bb))
_math_lcm.__nc_callable__ = True

def _math_script(a, b):
    return (_math_dec(a) + _math_dec(b)) / decimal.Decimal(2)
_math_script.__nc_callable__ = True


def _math_base_env(interp: "NCInterpreter") -> Dict[str, Any]:
    env = interp.base_env()
    # high precision (no rounding output enforced; Decimal will keep context)
    ctx = decimal.getcontext()
    ctx.prec = max(ctx.prec, 120)

    env["D"] = decimal.Decimal
    env["script"] = _math_script

    # constants
    env["pi"] = decimal.Decimal(str(_pymath.pi))
    env["e"] = decimal.Decimal(str(_pymath.e))

    # core funcs
    env["abs"] = abs
    env["min"] = min
    env["max"] = max
    env["sum"] = sum

    # math funcs (float-based but returns Decimal)
    env["sqrt"] = _math_sqrt
    env["sin"] = _math_wrap_unary(_pymath.sin)
    env["cos"] = _math_wrap_unary(_pymath.cos)
    env["tan"] = _math_wrap_unary(_pymath.tan)
    env["asin"] = _math_wrap_unary(_pymath.asin)
    env["acos"] = _math_wrap_unary(_pymath.acos)
    env["atan"] = _math_wrap_unary(_pymath.atan)
    env["log"] = _math_wrap_binary(_pymath.log)
    env["ln"] = _math_wrap_unary(_pymath.log)
    env["exp"] = _math_wrap_unary(_pymath.exp)
    env["floor"] = _math_wrap_unary(_pymath.floor)
    env["ceil"] = _math_wrap_unary(_pymath.ceil)
    env["round"] = round
    env["fact"] = _math_fact
    env["gcd"] = _math_gcd
    env["lcm"] = _math_lcm

    return env


def _math_eval(expr: str, env: Dict[str, Any], interp: "NCInterpreter") -> Any:
    expr = (expr or "").strip()
    expr = _math_apply_script_operator(expr)
    expr = _math_decimalize_numbers(expr)
    return safe_eval_expr(expr, env, interp.policy)


class _MathParser:
    """
    Very small indentation parser (2 spaces = 1 level) for MathNC.
    Produces a list of dict statements.
    """
    def __init__(self, text: str, source_name: str):
        self.lines = (text or "").splitlines()
        self.source = _format_source(source_name)

    def _indent(self, raw: str) -> int:
        # tabs forbidden
        if "\t" in raw:
            raise NCError(self.source, 1, "Tabs not allowed (use 2 spaces)")
        return len(raw) - len(raw.lstrip(" "))

    def parse(self) -> List[dict]:
        out: List[dict] = []
        i = 0

        # skip trigger line(s)
        while i < len(self.lines):
            raw = self.lines[i]
            s = (raw.split("#", 1)[0]).strip()
            if not s:
                i += 1
                continue
            low = s.lower()
            if low in _MATH_TRIGGER_LINES or any(low.startswith(p) for p in _MATH_TRIGGER_PREFIXES):
                i += 1
            break

        while i < len(self.lines):
            raw = self.lines[i]
            ln = i + 1
            stripped = raw.split("#", 1)[0].rstrip()
            if not stripped.strip():
                i += 1
                continue

            ind = self._indent(raw)
            if ind % 2 != 0:
                raise NCError(self.source, ln, "Indent must be multiple of 2 spaces")

            s = stripped.strip()

            m = re.match(r"import\s+([A-Za-z_]\w*)$", s)
            if m:
                out.append({"kind": "import", "name": m.group(1), "line": ln})
                i += 1
                continue

            m = re.match(r"export\s+([A-Za-z_]\w*)$", s)
            if m:
                out.append({"kind": "export", "name": m.group(1), "line": ln})
                i += 1
                continue

            m = re.match(r"export\s+--all$", s)
            if m:
                out.append({"kind": "export_all", "line": ln})
                i += 1
                continue

            m = re.match(r"let\s+([A-Za-z_]\w*)\s*=\s*(.+)$", s)
            if m:
                out.append({"kind": "let", "name": m.group(1), "expr": m.group(2), "line": ln})
                i += 1
                continue

            m = re.match(r"set\s+([A-Za-z_]\w*)\s*=\s*(.+)$", s)
            if m:
                out.append({"kind": "set", "name": m.group(1), "expr": m.group(2), "line": ln})
                i += 1
                continue

            m = re.match(r"fn\s+([A-Za-z_]\w*)\s*\(([^)]*)\)\s*:\s*$", s)
            if m:
                name = m.group(1)
                args_raw = m.group(2).strip()
                args = []
                if args_raw:
                    args = [a.strip() for a in _split_commas(args_raw)]
                # parse indented body
                body: List[dict] = []
                i += 1
                while i < len(self.lines):
                    raw2 = self.lines[i]
                    ln2 = i + 1
                    s2 = raw2.split("#", 1)[0].rstrip()
                    if not s2.strip():
                        i += 1
                        continue
                    ind2 = self._indent(raw2)
                    if ind2 <= ind:
                        break
                    if ind2 != ind + 2:
                        raise NCError(self.source, ln2, "MathNC fn body must be indented by exactly +2 spaces")
                    stmt = s2.strip()
                    mm = re.match(r"(ret|return)\s+(.+)$", stmt)
                    if mm:
                        body.append({"kind": "return", "expr": mm.group(2), "line": ln2})
                    else:
                        # expression statement
                        body.append({"kind": "expr", "expr": stmt, "line": ln2})
                    i += 1
                out.append({"kind": "fn", "name": name, "args": args, "body": body, "line": ln})
                continue

            # default: expression line
            out.append({"kind": "expr", "expr": s, "line": ln})
            i += 1

        return out


def run_math_text(
    nc_text: str,
    base: str = ".",
    extra_paths: Optional[List[str]] = None,
    policy: Optional[NCPolicy] = None,
    enable_ui: bool = True,
    source_name: str = "<text>",
) -> Dict[str, Any]:
    # Auto-detect MathNC
    if _math_is_triggered(nc_text):
        return run_math_text(
            nc_text,
            base=base,
            extra_paths=extra_paths,
            policy=policy,
            enable_ui=enable_ui,
            source_name=source_name,
        )

    interp = NCInterpreter(policy=policy, enable_ui=enable_ui)
    env = _math_base_env(interp)
    exports: List[str] = []

    # allow imports from ./modul like python
    # (NC already supports standard search paths; we add "<base>/modul" automatically for MathNC)
    extra_paths = list(extra_paths or [])
    if not _is_url(base):
        extra_paths.append(os.path.join(os.path.abspath(base), "modul"))

    # reuse interpreter module loader
    interp._extra_paths = extra_paths  # type: ignore[attr-defined]

    parser = _MathParser(nc_text, source_name=source_name)
    stmts = parser.parse()

    last_value = None
    for st in stmts:
        kind = st["kind"]
        ln = int(st.get("line", 1))
        try:
            if kind == "import":
                name = st["name"]
                mod = interp.load_module(
                    name,
                    base=base,
                    extra_paths=extra_paths,
                    caller_source=source_name,
                    caller_line=ln,
                )
                env[name] = mod
                continue

            if kind == "export":
                if st["name"] not in exports:
                    exports.append(st["name"])
                continue

            if kind == "export_all":
                base_keys = set(env.get("__export_base_keys__") or [])
                for name in env.keys():
                    if str(name).startswith("__"):
                        continue
                    if name in base_keys:
                        continue
                    if name not in exports:
                        exports.append(name)
                continue

            if kind == "let":
                env[st["name"]] = _math_eval(st["expr"], env, interp)
                last_value = env[st["name"]]
                continue

            if kind == "set":
                if st["name"] not in env:
                    env[st["name"]] = None
                env[st["name"]] = _math_eval(st["expr"], env, interp)
                last_value = env[st["name"]]
                continue

            if kind == "fn":
                # Create an NC function wrapper with MathNC body execution
                fn_name = st["name"]
                arg_names = list(st.get("args") or [])
                body = list(st.get("body") or [])

                def _make_math_fn(fn_name, arg_names, body, closure):
                    def _call(*args):
                        if len(args) != len(arg_names):
                            raise RuntimeError(f"{fn_name} expected {len(arg_names)} args, got {len(args)}")
                        local = dict(closure)
                        for k, v in zip(arg_names, args):
                            local[k] = v
                        last = None
                        for bst in body:
                            bkind = bst["kind"]
                            if bkind == "return":
                                return _math_eval(bst["expr"], local, interp)
                            if bkind == "expr":
                                last = _math_eval(bst["expr"], local, interp)
                                local["_"] = last
                                continue
                        return last
                    _call.__nc_callable__ = True
                    return _call

                env[fn_name] = _make_math_fn(fn_name, arg_names, body, dict(env))
                last_value = env[fn_name]
                continue

            if kind == "expr":
                last_value = _math_eval(st["expr"], env, interp)
                env["_"] = last_value
                print(str(last_value))
                continue

        except NCError:
            raise
        except Exception as e:
            raise NCError(_format_source(source_name), ln, str(e), import_stack=getattr(interp, "_import_stack", None))

    # expose exports similar to NC modules: return as dict
    if exports:
        exported = {k: env.get(k) for k in exports}
    else:
        exported = {}
    return {"env": env, "exports": exported, "last": last_value}


# ============================================================
# Runtime: function, module, control flow
# ============================================================

class NCBreak(Exception):
    pass


class NCContinue(Exception):
    pass


class NCReturn(Exception):
    def __init__(self, value: Any):
        self.value = value


class NCEnd(Exception):
    pass


@dataclass
class NCFn:
    name: str
    arg_names: List[str]
    body: List["Stmt"]
    closure: Dict[str, Any]
    interp: "NCInterpreter"
    source: str = "<text>"
    line: int = 1
    base_dir: str = "."

    def call(self, args: List[Any], runtime_env: Optional[Dict[str, Any]] = None) -> Any:
        if len(args) != len(self.arg_names):
            raise RuntimeError(f"{self.name} expected {len(self.arg_names)} args, got {len(args)}")
        local = dict(self.closure)

        # BUGFIX:
        # Functions previously executed only with the environment snapshot that
        # existed at definition time. That broke later-updated globals like
        # checkmarks / settings variables inside button actions.
        #
        # Merge the current runtime environment on top of the stored closure so
        # newer global values are visible, while still keeping function-local
        # assignments isolated to this call.
        if runtime_env:
            try:
                for k, v in runtime_env.items():
                    local[k] = v
            except Exception:
                pass

        for k, v in zip(self.arg_names, args):
            local[k] = v
        self.interp._function_depth = int(getattr(self.interp, "_function_depth", 0)) + 1
        try:
            with self.interp._with_source(self.source, push_stack=False):
                self.interp.exec_block(
                    self.body,
                    local,
                    base_dir=self.base_dir,
                    source_name=self.source,
                )
        except NCReturn as r:
            return r.value
        except NCError as error:
            error.add_frame(self.name, self.source, self.line)
            raise
        finally:
            self.interp._function_depth = max(0, int(getattr(self.interp, "_function_depth", 1)) - 1)
        return None


class NCModule:
    def __init__(self, name: str):
        self.name = name
        self.namespace: Dict[str, Any] = {}
        self.exports: Dict[str, Any] = {}

    def set(self, k: str, v: Any):
        self.namespace[k] = v

    def get(self, k: str) -> Any:
        if k in self.exports:
            return self.exports[k]
        raise KeyError(f"Module '{self.name}' has no export '{k}'")

    def finalize_exports(self, export_names: List[str]):
        seen = set()
        ordered: List[str] = []
        for k in export_names:
            if k in self.namespace and k not in seen:
                seen.add(k)
                ordered.append(k)
        self.exports = {k: self.namespace.get(k) for k in ordered}


# ============================================================
# UI bridge for t_windows via "__TWIN__ {json}"
# ============================================================

def twin_emit(payload: Dict[str, Any]) -> None:
    print("__TWIN__ " + json.dumps(payload, ensure_ascii=False))


class UIBridge:
    def __init__(self, enabled: bool = True):
        self.enabled = enabled
        self.win_id = "nc_sim"
        self.opened = False

    def window(self, title: str, w: int, h: int, win_id: str = "nc_sim"):
        if not self.enabled:
            return
        self.win_id = win_id
        self.opened = True
        twin_emit({"cmd": "window.open", "id": win_id, "title": title, "w": int(w), "h": int(h)})

    def plot(self, series: str, value: float, step: int):
        if not self.enabled or not self.opened:
            return
        twin_emit({"cmd": "plot.add", "id": self.win_id, "series": series, "step": int(step), "value": float(value)})

    def table(self, name: str, rows: List[List[Any]]):
        if not self.enabled or not self.opened:
            return
        twin_emit({"cmd": "table.set", "id": self.win_id, "name": name, "rows": rows})

    def html_set(self, html: str, title: str = "NC", w: int = 1000, h: int = 700, win_id: str = "nc_sim"):
        if not self.enabled:
            return
        # If no window was opened yet, open one and push HTML into the HTML tab
        if not self.opened:
            self.win_id = win_id
            self.opened = True
        twin_emit({"cmd": "window.open", "id": self.win_id, "title": str(title), "w": int(w), "h": int(h), "content_html": str(html)})


    def scene_set(self, scene: Dict[str, Any], title: str = "NC", w: int = 1000, h: int = 700, win_id: str = "nc_sim"):
        """Render NCUI2 scene graph in the host (no HTML/CSS)."""
        if not self.enabled:
            return
        if not self.opened:
            self.win_id = win_id
            self.opened = True
        twin_emit({"cmd": "ui.scene", "id": self.win_id, "title": str(title), "w": int(w), "h": int(h), "scene": scene})



    # -----------------------------
    # Camera bridge (host-provided)
    # These commands are NO-OP unless your nc_twin_run host implements them.
    # -----------------------------
    def camera_open(self, device: str = "default", facing: str = "back", w: int = 1280, h: int = 720, fps: int = 30):
        if not self.enabled:
            return
        if not self.opened:
            self.window("NC Camera", 1000, 700, win_id=self.win_id or "nc_cam")
        twin_emit({"cmd": "camera.open", "id": self.win_id, "device": str(device), "facing": str(facing), "w": int(w), "h": int(h), "fps": int(fps)})

    def camera_close(self):
        if not self.enabled or not self.opened:
            return
        twin_emit({"cmd": "camera.close", "id": self.win_id})

    def camera_snap(self, path: str = "snap.jpg"):
        if not self.enabled or not self.opened:
            return
        twin_emit({"cmd": "camera.snap", "id": self.win_id, "path": str(path)})

    def camera_record_start(self, path: str = "video.mp4", w: int = 1280, h: int = 720, fps: int = 30):
        if not self.enabled or not self.opened:
            return
        twin_emit({"cmd": "camera.record_start", "id": self.win_id, "path": str(path), "w": int(w), "h": int(h), "fps": int(fps)})

    def camera_record_stop(self):
        if not self.enabled or not self.opened:
            return
        twin_emit({"cmd": "camera.record_stop", "id": self.win_id})

    def camera_set(self, key: str, value: Any):
        if not self.enabled or not self.opened:
            return
        twin_emit({"cmd": "camera.set", "id": self.win_id, "key": str(key), "value": value})


# ============================================================
# World model (Module A)
# ============================================================

class LinearWorldModel:
    def __init__(self, state_keys: List[str], actions: List[str], lr: float = 0.05):
        self.state_keys = state_keys
        self.actions = actions
        self.lr = float(lr)
        self.d = len(state_keys) + len(actions) + 1
        self.W = {k: [0.0] * self.d for k in state_keys}

    def _one_hot(self, a: str) -> List[float]:
        return [1.0 if x == a else 0.0 for x in self.actions]

    def featurize(self, s: Dict[str, float], action: str) -> List[float]:
        x = [float(s[k]) for k in self.state_keys]
        x.extend(self._one_hot(action))
        x.append(1.0)
        return x

    def predict(self, s: Dict[str, float], action: str) -> Dict[str, float]:
        x = self.featurize(s, action)
        out = {}
        for k in self.state_keys:
            w = self.W[k]
            y = 0.0
            for i in range(self.d):
                y += w[i] * x[i]
            out[k] = y
        return out

    def update(self, s: Dict[str, float], action: str, s_next: Dict[str, float]) -> float:
        x = self.featurize(s, action)
        err = 0.0
        for k in self.state_keys:
            w = self.W[k]
            y = 0.0
            for i in range(self.d):
                y += w[i] * x[i]
            e = float(s_next[k]) - y
            err += abs(e)
            for i in range(self.d):
                w[i] += self.lr * e * x[i]
        return err / max(1, len(self.state_keys))


@dataclass
class WorldDef:
    name: str
    state_init: Dict[str, float]
    actions: List[str]
    bounds: Dict[str, Tuple[float, float]]
    step_body: List["Stmt"]


class AgentConfig:
    def __init__(self):
        self.world_name: Optional[str] = None
        self.lr = 0.05
        self.curiosity = 1.0
        self.horizon = 6
        self.samples = 30
        self.steps = 200
        self.log_vars: List[str] = []
        self.tick = False


# ============================================================
# Parser
# ============================================================

@dataclass
class Stmt:
    kind: str
    data: Dict[str, Any]
    line: int


def _indent_level(raw: str) -> int:
    """
    2 spaces = 1 indent.
    IMPORTANT: raw must still contain leading spaces (indentation).
    Reject tabs / non-space indentation for determinism.
    """
    prefix = raw[: len(raw) - len(raw.lstrip())]
    if "\t" in prefix:
        raise SyntaxError("Indentation must use spaces only (tabs are not allowed)")
    for ch in prefix:
        if ch != " ":
            raise SyntaxError("Indentation must use normal spaces only")
    sp = len(raw) - len(raw.lstrip(" "))
    if sp % 2 != 0:
        raise SyntaxError("Indent must be multiples of 2 spaces")
    return sp // 2


def _strip_comment(line: str) -> str:
    out, q = [], None
    i = 0
    while i < len(line):
        ch = line[i]
        if q:
            out.append(ch)
            if ch == q:
                q = None
            i += 1
            continue
        if ch in ("'", '"'):
            q = ch
            out.append(ch)
            i += 1
            continue
        if ch == "#":
            break
        out.append(ch)
        i += 1
    return "".join(out).rstrip()


_NC_ALIASABLE_KEYWORDS = {
    # control flow / statements
    "if", "elif", "else", "repeat", "while", "for", "fn",
    "return", "ret", "break", "continue",
    "print", "let", "set", "import", "from", "as", "export",
    "run", "window", "size", "plot", "table", "tick",
    "pick", "text", "html", "css", "use", "anim",
    "world", "agent", "state", "actions", "bounds", "step",
    "render", "ren", "times",
    "button", "botton", "knopf", "action", "color", "end", "input",
    "checkmark", "checkbox", "check", "haken", "haekchen", "häckchen", "on", "off",
    "textcolor", "textcollor", "textcolour", "fontcolor", "printcolor",

    # logical / membership / identity operators
    "is", "is not", "in", "not in", "and", "or", "not",

    # comparison operators (aliasable too)
    "==", "!=", ">", "<", ">=", "<=",
}

def _is_ident_char(ch: str) -> bool:
    return ch.isalnum() or ch == "_"

def _boundary_ok(text: str, start: int, end: int) -> bool:
    before = text[start - 1] if start > 0 else ""
    after = text[end] if end < len(text) else ""
    left_ok = (not before) or (not _is_ident_char(before))
    right_ok = (not after) or (not _is_ident_char(after))
    return left_ok and right_ok

def _replace_aliases_outside_strings(text: str, alias_to_canonical: Dict[str, str]) -> str:
    if not text or not alias_to_canonical:
        return text

    keys = sorted(alias_to_canonical.keys(), key=lambda x: (-len(x), x))
    out: List[str] = []
    i = 0
    q = None

    while i < len(text):
        ch = text[i]
        if q:
            out.append(ch)
            if ch == q:
                q = None
            i += 1
            continue
        if ch in ("'", '"'):
            q = ch
            out.append(ch)
            i += 1
            continue

        matched = False
        for alias in keys:
            if text.startswith(alias, i):
                j = i + len(alias)
                if _boundary_ok(text, i, j):
                    out.append(alias_to_canonical[alias])
                    i = j
                    matched = True
                    break
        if matched:
            continue

        out.append(ch)
        i += 1

    return "".join(out)

def _parse_keyword_alias_line(s: str) -> Optional[Tuple[str, str]]:
    if "=" not in s:
        return None
    left, right = s.split("=", 1)
    left = " ".join(left.strip().split()).lower()
    right = " ".join(right.strip().split()).lower()
    if not left or not right:
        return None
    if left not in _NC_ALIASABLE_KEYWORDS:
        return None
    if left == right:
        return None
    return left, right

def _normalize_alias_mapping(keyword_aliases: Dict[str, str]) -> Dict[str, str]:
    alias_to_canonical: Dict[str, str] = {}
    for canonical, alias in (keyword_aliases or {}).items():
        c = " ".join(str(canonical).strip().split()).lower()
        a = " ".join(str(alias).strip().split()).lower()
        if not c or not a or c == a:
            continue
        if c not in _NC_ALIASABLE_KEYWORDS:
            continue
        alias_to_canonical[a] = c
    return alias_to_canonical






# ============================================================
# Console color + button helpers
# ============================================================

_ANSI_COLOR_NAMES = {
    "black": (0, 0, 0),
    "red": (220, 60, 60),
    "green": (60, 200, 120),
    "yellow": (230, 200, 70),
    "blue": (80, 150, 255),
    "magenta": (220, 100, 255),
    "cyan": (80, 220, 230),
    "white": (245, 245, 245),
    "gray": (150, 150, 150),
    "grey": (150, 150, 150),
    "orange": (255, 160, 60),
    "purple": (170, 110, 255),
    "pink": (255, 120, 190),
}

def _ansi_from_color(value: Any, *, background: bool = False) -> str:
    if value is None:
        return ""
    s = str(value).strip()
    if not s:
        return ""
    lower = s.lower()
    rgb = None
    if lower in _ANSI_COLOR_NAMES:
        rgb = _ANSI_COLOR_NAMES[lower]
    elif re.fullmatch(r"#?[0-9a-fA-F]{6}", s):
        s2 = s[1:] if s.startswith("#") else s
        rgb = (int(s2[0:2], 16), int(s2[2:4], 16), int(s2[4:6], 16))
    elif "," in s:
        parts = [p.strip() for p in s.split(",")]
        if len(parts) == 3:
            try:
                rgb = tuple(max(0, min(255, int(float(p)))) for p in parts)
            except Exception:
                rgb = None
    if rgb is None:
        return ""
    mode = 48 if background else 38
    return f"\033[{mode};2;{rgb[0]};{rgb[1]};{rgb[2]}m"

def _ansi_reset() -> str:
    return "\033[0m"

_WINDOWS_ANSI_ENABLED: Optional[bool] = None

def _enable_windows_ansi() -> bool:
    global _WINDOWS_ANSI_ENABLED
    if _WINDOWS_ANSI_ENABLED is not None:
        return bool(_WINDOWS_ANSI_ENABLED)
    if os.name != "nt":
        _WINDOWS_ANSI_ENABLED = True
        return True
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
        ENABLE_PROCESSED_OUTPUT = 0x0001
        for handle_id in (-11, -12):  # stdout, stderr
            handle = kernel32.GetStdHandle(handle_id)
            if handle in (0, -1):
                continue
            mode = ctypes.c_uint()
            if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                kernel32.SetConsoleMode(handle, mode.value | ENABLE_VIRTUAL_TERMINAL_PROCESSING | ENABLE_PROCESSED_OUTPUT)
        _WINDOWS_ANSI_ENABLED = True
        return True
    except Exception:
        _WINDOWS_ANSI_ENABLED = False
        return False

def _stdout_supports_ansi() -> bool:
    if not _stdout_is_tty():
        return False
    if os.name != "nt":
        return True
    if os.environ.get("WT_SESSION") or os.environ.get("ANSICON"):
        return True
    term = (os.environ.get("TERM") or "").lower()
    if any(x in term for x in ("xterm", "ansi", "vt100", "cygwin")):
        return True
    return _enable_windows_ansi()

def _stdin_is_tty() -> bool:
    try:
        return bool(sys.stdin.isatty())
    except Exception:
        return False

def _stdout_is_tty() -> bool:
    try:
        return bool(sys.stdout.isatty())
    except Exception:
        return False

def _console_print_colored(text: str, color: Any = None, end: str = "\n") -> None:
    msg = "" if text is None else str(text)
    if color is not None and _stdout_supports_ansi():
        code = _ansi_from_color(color)
        if code:
            print(f"{code}{msg}{_ansi_reset()}", end=end)
            return
    print(msg, end=end)

def _console_render_picker_lines(items: List[dict], selected: int) -> List[str]:
    lines: List[str] = []
    for idx, item in enumerate(items):
        prefix = ">" if idx == selected else " "
        kind = str(item.get("kind") or "button")
        label = str(item.get("label") or ("Checkmark" if kind == "checkmark" else "Button"))
        color = item.get("color")
        color_code = _ansi_from_color(color) if (_stdout_supports_ansi() and color is not None) else ""
        reset = _ansi_reset() if color_code else ""
        if kind == "checkmark":
            checked = bool(item.get("checked"))
            mark = "✓" if checked else " "
            base = f"{prefix} [{mark}] {label}"
        else:
            base = f"{prefix} {label}"
        if idx == selected and _stdout_supports_ansi():
            lines.append(f"\033[7m{color_code}{base}{reset}\033[0m")
        else:
            lines.append(f"{color_code}{base}{reset}")
    return lines


def _console_render_inline_row(items: List[dict], selected: int) -> str:
    """Render a horizontal row; ``selected`` is a selectable-item index."""
    parts: List[str] = []
    selectable_index = 0
    for item in items:
        kind = str(item.get("kind") or "button")
        color = item.get("color")
        color_code = _ansi_from_color(color) if (_stdout_supports_ansi() and color is not None) else ""
        reset = _ansi_reset() if color_code else ""

        if kind == "print":
            text = " ".join(str(value) for value in item.get("values") or [])
            parts.append(f"{color_code}{text}{reset}")
            continue

        if kind == "checkmark":
            mark = "✓" if bool(item.get("checked")) else " "
            text = f"[{mark}] {item.get('label', 'Checkmark')}"
        elif kind == "input":
            text = f"[✎ {item.get('prompt', 'Input')}]"
        else:
            text = str(item.get("label") or "Button")

        if selectable_index == selected:
            text = f"> {text}"
            if _stdout_supports_ansi():
                text = f"\033[7m{color_code}{text}{reset}\033[0m"
            elif color_code:
                text = f"{color_code}{text}{reset}"
        else:
            text = f"  {text}"
            if color_code:
                text = f"{color_code}{text}{reset}"
        parts.append(text)
        selectable_index += 1
    return "    ".join(parts)

def _console_read_key() -> str:
    if not _stdin_is_tty():
        return "enter"
    try:
        import msvcrt  # type: ignore
        ch = msvcrt.getwch()
        if ch in ("\r", "\n"):
            return "enter"
        if ch in (" ",):
            return "enter"
        if ch == "\x03":
            raise KeyboardInterrupt
        if ch == "\xe0":
            ch2 = msvcrt.getwch()
            return {"H": "up", "P": "down", "K": "left", "M": "right"}.get(ch2, "")
        return {"w": "up", "W": "up", "s": "down", "S": "down"}.get(ch, "")
    except ImportError:
        import termios
        import tty
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            ch = sys.stdin.read(1)
            if ch in ("\r", "\n", " "):
                return "enter"
            if ch == "\x03":
                raise KeyboardInterrupt
            if ch == "\x1b":
                nxt = sys.stdin.read(1)
                if nxt == "[":
                    nxt2 = sys.stdin.read(1)
                    return {"A": "up", "B": "down", "C": "right", "D": "left"}.get(nxt2, "")
                return ""
            return {"w": "up", "W": "up", "s": "down", "S": "down"}.get(ch, "")
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)

def _console_choose_button(buttons: List[dict]) -> int:
    if not buttons:
        return -1
    if len(buttons) == 1 and not _stdin_is_tty():
        return 0
    selected = 0
    while True:
        lines = _console_render_picker_lines(buttons, selected)
        print()
        for line in lines:
            print(line)
        key = _console_read_key()
        if key in ("up", "left"):
            selected = (selected - 1) % len(buttons)
        elif key in ("down", "right"):
            selected = (selected + 1) % len(buttons)
        elif key == "enter" or not key:
            return selected
        if _stdout_is_tty():
            print(f"\033[{len(lines)+1}A", end="")

@dataclass
class NCConsoleButton:
    label_expr: str
    action_body: List[Any] = field(default_factory=list)
    color_expr: Optional[str] = None


@dataclass
class NCConsoleCheckmark:
    var_name: str
    label_expr: str
    color_expr: Optional[str] = None


# ============================================================
# UI Mini-DSL (pick/text/anim/ren)
# - "text <name>:" defines a reusable CSS class: .t_<name>
# - "anim <name>:" defines @keyframes <name>
# - "pick <selector>:" creates/updates a node (CSS-ish selector) and allows:
#     text <expr>
#     html <expr>
#     css  <expr>        (CSS declarations or full rule, appended)
#     use  <name>        (apply text style .t_<name>)
#     anim <name> <expr> (e.g. anim fadeIn "0.6s ease both")
# - "ren ..." renders the current doc via ui.html_set(...)
#
# This is intentionally small + sandbox-safe: no DOM, just HTML/CSS generation.
# ============================================================

def _ui_escape(s: Any) -> str:
    try:
        import html as _html
        return _html.escape("" if s is None else str(s), quote=True)
    except Exception:
        return "" if s is None else str(s)


def _ui_parse_color(v: str):
    s = (v or "").strip()
    if not s:
        return None, None
    # rgba(r,g,b,a)
    m = re.match(r"rgba\((\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*([0-9.]+)\)", s, re.I)
    if m:
        return [int(m.group(1)), int(m.group(2)), int(m.group(3))], float(m.group(4))
    m = re.match(r"rgb\((\d+)\s*,\s*(\d+)\s*,\s*(\d+)\)", s, re.I)
    if m:
        return [int(m.group(1)), int(m.group(2)), int(m.group(3))], None
    # #rrggbb
    m = re.match(r"#?([0-9a-fA-F]{6})$", s)
    if m:
        hx = m.group(1)
        return [int(hx[0:2],16), int(hx[2:4],16), int(hx[4:6],16)], None
    return None, None

def _ui_parse_decls(decls: str) -> dict:
    """Parse CSS-like declarations into a neutral NCUI2 property dict (NOT CSS)."""
    out = {}
    for part in (decls or "").split(";"):
        p = part.strip()
        if not p or ":" not in p:
            continue
        k, v = p.split(":", 1)
        key = k.strip().lower().replace("-", "_")
        val = v.strip()
        if key in ("font_size", "font_size_px"):
            m = re.match(r"([0-9.]+)px$", val)
            if m:
                px = float(m.group(1))
                out["font_size"] = px * 0.75  # rough px->pt
            else:
                try:
                    out["font_size"] = float(val)
                except Exception:
                    pass
            continue
        if key == "font_weight":
            if val.isdigit():
                out["font_weight"] = int(val)
            elif val.lower() == "bold":
                out["font_weight"] = 75
            continue
        if key == "opacity":
            try:
                out["opacity"] = float(val)
            except Exception:
                pass
            continue
        if key == "color":
            col, op = _ui_parse_color(val)
            if col:
                out["color"] = col
            if op is not None:
                out["opacity"] = op
            continue
        # generic numeric px
        m = re.match(r"([0-9.]+)px$", val)
        if m:
            try:
                out[key] = float(m.group(1))
                continue
            except Exception:
                pass
        out[key] = val
    return out

class _UIDoc:
    """Internal UI document for the NC mini-DSL (pick/text/anim/ren).

    It can render either:
      - NCUI2 scene graph (preferred, no HTML/CSS needed in user code), or
      - HTML/CSS (fallback / debug)
    """

    def __init__(self):
        self.reset()

    def reset(self):
        self.title = "NC"
        self.w = 1000
        self.h = 700
        # NCUI2 styles/anims (neutral props, NOT CSS)
        self.text_styles: Dict[str, dict] = {}   # name -> props
        self.anims: Dict[str, dict] = {}         # name -> anim meta
        # nodes: [{tag,id,classes,text,html,props,use,anim,css}]
        self.nodes: List[dict] = []
        self.global_css: List[str] = []

    def _ensure_node(self, selector: str) -> dict:
        sel = (selector or "").strip()
        tag = "div"
        node_id = ""
        classes: List[str] = []

        if sel.startswith("#"):
            node_id = sel[1:].strip()
        elif sel.startswith("."):
            classes = [c for c in sel[1:].strip().split(".") if c]
        else:
            # tag or tag#id or tag.class
            m = re.match(r"^([A-Za-z][\w-]*)(.*)$", sel)
            if m:
                tag = m.group(1)
                rest = (m.group(2) or "").strip()
                if rest.startswith("#"):
                    node_id = rest[1:].strip()
                elif rest.startswith("."):
                    classes = [c for c in rest[1:].strip().split(".") if c]

        # find existing node by id, else by first class, else append new
        if node_id:
            for n in self.nodes:
                if n.get("id") == node_id:
                    return n
        if classes:
            for n in self.nodes:
                if classes[0] in (n.get("classes") or []):
                    return n

        n = {
            "tag": tag,
            "id": node_id,
            "classes": classes,
            "text": None,
            "html": None,
            "props": {},
            "use": None,
            "anim": None,
            "css": [],
        }
        self.nodes.append(n)
        return n

    def add_text_style(self, name: str, decls: str):
        key = str(name).strip()
        if not key:
            return
        self.text_styles[key] = _ui_parse_decls((decls or "").strip())

    def add_anim(self, name: str, body: str):
        key = str(name).strip()
        if not key:
            return
        # Minimal keyframe parsing: detect opacity 0->1, duration heuristic.
        b = (body or "").strip()
        meta = {"opacity_from": 0.0, "opacity_to": 1.0, "duration_ms": 600}
        if "opacity" in b:
            if re.search(r"opacity\s*:\s*0", b):
                meta["opacity_from"] = 0.0
            if re.search(r"opacity\s*:\s*1", b):
                meta["opacity_to"] = 1.0
        self.anims[key] = meta

    def pick(self, selector: str) -> dict:
        return self._ensure_node(selector)

    # -------- Renderers --------

    def render_scene(self) -> dict:
        """Produce a neutral scene graph consumed by the NCW host UI tab."""
        nodes_out = []
        for n in self.nodes:
            anim_name = None
            if isinstance(n.get("anim"), dict):
                anim_name = n["anim"].get("name")
            elif isinstance(n.get("anim"), str):
                anim_name = n.get("anim")

            nodes_out.append(
                {
                    "tag": n.get("tag") or "text",
                    "id": n.get("id") or "",
                    "classes": list(n.get("classes") or []),
                    "text": n.get("text"),
                    "html": n.get("html"),
                    "props": dict(n.get("props") or {}),
                    "use": n.get("use"),
                    "anim": anim_name,
                }
            )

        return {
            "title": self.title,
            "w": int(self.w),
            "h": int(self.h),
            "styles": dict(self.text_styles),
            "anims": dict(self.anims),
            "nodes": nodes_out,
        }

    def render_html(self) -> str:
        css_parts: List[str] = []

        # NOTE: this HTML renderer is only a fallback / debug view.
        # text_styles/anims here are raw CSS strings, but in our NCUI2 path we store props.
        for name, props in self.text_styles.items():
            # best-effort: convert a few common props back to CSS
            if isinstance(props, dict):
                decls = []
                if "color" in props and isinstance(props["color"], list) and len(props["color"]) == 3:
                    r, g, b = props["color"]
                    a = float(props.get("alpha", 1.0))
                    decls.append(f"color: rgba({r},{g},{b},{a});")
                if "font_size" in props:
                    decls.append(f"font-size: {float(props['font_size']):.2f}px;")
                css_parts.append(f".t_{name}{{{''.join(decls)}}}")

        for name, _meta in self.anims.items():
            # no full keyframe export for NCUI2 meta; keep empty
            css_parts.append(f"@keyframes {name}{{ from{{opacity:0}} to{{opacity:1}} }}")

        css_parts.extend(self.global_css)

        # per-node css rules
        for n in self.nodes:
            sel = ""
            if n.get("id"):
                sel = "#" + str(n["id"])
            elif n.get("classes"):
                sel = "." + str(n["classes"][0])
            else:
                sel = n.get("tag") or "div"

            for rule in n.get("css") or []:
                rr = str(rule).strip()
                if not rr:
                    continue
                # if looks like full rule, keep it; else wrap into selector {..}
                if "{" in rr and "}" in rr:
                    css_parts.append(rr)
                else:
                    css_parts.append(f"{sel}{{{rr}}}")

        head = "<meta charset='utf-8'/>"
        style = "<style>\n" + "\n".join(css_parts) + "\n</style>"

        base_css = """
        <style>
          body{margin:0;font-family:system-ui,-apple-system,Segoe UI,Roboto,Arial;background:#0b1020;color:rgba(255,255,255,.92);}
          .nc_wrap{min-height:100vh;padding:18px;box-sizing:border-box;
            background: radial-gradient(1100px circle at 18% 15%, rgba(90,200,255,.16), transparent 55%),
                        radial-gradient(900px circle at 82% 40%, rgba(255,180,220,.10), transparent 60%),
                        radial-gradient(1100px circle at 55% 92%, rgba(170,255,210,.08), transparent 60%),
                        linear-gradient(180deg, rgba(10,16,32,.95), rgba(8,10,18,.92));
          }
          .nc_card{max-width:980px;margin:0 auto;border-radius:22px;padding:16px;
            background:rgba(255,255,255,.06);border:1px solid rgba(255,255,255,.12);
            backdrop-filter: blur(14px); box-shadow:0 20px 65px rgba(0,0,0,.25);
          }
        </style>
        """

        parts: List[str] = []
        parts.append("<div class='nc_wrap'><div class='nc_card'>")
        for n in self.nodes:
            tag = (n.get("tag") or "div").strip() or "div"
            attrs = []
            if n.get("id"):
                attrs.append(f"id='{_ui_escape(n['id'])}'")
            cls = list(n.get("classes") or [])
            if n.get("use"):
                cls.append(f"t_{_ui_escape(n['use'])}")
            if cls:
                attrs.append(f"class='{_ui_escape(' '.join(cls))}'")
            inner = ""
            if n.get("html") is not None:
                inner = str(n.get("html") or "")
            elif n.get("text") is not None:
                inner = _ui_escape(n.get("text"))
            parts.append(f"<{tag} {' '.join(attrs)}>{inner}</{tag}>")
        parts.append("</div></div>")
        body = "\n".join(parts)

        return f"<!doctype html><html><head>{head}{base_css}{style}<title>{_ui_escape(self.title)}</title></head><body>{body}</body></html>"


def _fold_logical_lines(text: str) -> List[Tuple[str, int]]:
    """Fold multi-line expressions like dict/list literals into one logical line.

    Bugfix-only behavior:
    - keeps the indent of the first physical line
    - joins continued lines with spaces
    - only folds while (), [] or {} stay open
    - ignores bracket chars inside strings
    """
    physical = (text or "").splitlines()
    out: List[Tuple[str, int]] = []

    buf: List[str] = []
    start_ln: Optional[int] = None
    depth_paren = 0
    depth_brack = 0
    depth_brace = 0
    quote: Optional[str] = None
    esc = False

    def feed_state(s: str) -> None:
        nonlocal depth_paren, depth_brack, depth_brace, quote, esc
        for ch in str(s):
            if quote:
                if esc:
                    esc = False
                    continue
                if ch == "\\":
                    esc = True
                    continue
                if ch == quote:
                    quote = None
                continue
            if ch in ('"', "'"):
                quote = ch
                continue
            if ch == '(':
                depth_paren += 1
            elif ch == ')':
                depth_paren = max(0, depth_paren - 1)
            elif ch == '[':
                depth_brack += 1
            elif ch == ']':
                depth_brack = max(0, depth_brack - 1)
            elif ch == '{':
                depth_brace += 1
            elif ch == '}':
                depth_brace = max(0, depth_brace - 1)

    for idx, raw in enumerate(physical, start=1):
        if start_ln is None:
            start_ln = idx
        buf.append(raw)
        feed_state(_strip_comment(raw))

        if quote is None and depth_paren == 0 and depth_brack == 0 and depth_brace == 0:
            out.append((" ".join(part.rstrip() for part in buf), start_ln))
            buf = []
            start_ln = None

    if buf:
        out.append((" ".join(part.rstrip() for part in buf), start_ln or 1))

    return out


class NCParser:
    """
    Parser that:
    - preserves indent while stripping comments
    - COLLECTS multiple errors (does not stop on first one)
    """

    def __init__(self, text: str, source_name: str = "<text>"):
        self.source_name = _format_source(source_name)
        _diagnostics.register_source(self.source_name, text)
        self.raw_lines = text.splitlines()
        self.logical_lines = _fold_logical_lines(text)
        self.keyword_aliases: Dict[str, str] = {}
        self.alias_to_canonical: Dict[str, str] = {}
        self.lines: List[Tuple[int, str, int]] = []  # (indent, content, lineno)
        self.errors: List[NCReportedError] = []

        for raw, idx in self.logical_lines:
            no_comment = _strip_comment(raw)
            stripped = no_comment.strip()
            if not stripped:
                continue

            alias_pair = _parse_keyword_alias_line(stripped)
            if alias_pair is not None:
                canonical, alias = alias_pair
                self.keyword_aliases[canonical] = alias
                continue

            try:
                ind = _indent_level(no_comment)
            except Exception as e:
                self.errors.append(NCReportedError(self.source_name, idx, str(e)))
                ind = 0

            self.lines.append((ind, stripped, idx))

        self.alias_to_canonical = _normalize_alias_mapping(self.keyword_aliases)
        if self.alias_to_canonical:
            new_lines: List[Tuple[int, str, int]] = []
            for ind, content, ln in self.lines:
                new_lines.append((ind, _replace_aliases_outside_strings(content, self.alias_to_canonical), ln))
            self.lines = new_lines

    def parse(self) -> List[Stmt]:
        stmts, i = self._parse_block(0, 0)
        if i < len(self.lines):
            for j in range(i, len(self.lines)):
                _ind, _s, _ln = self.lines[j]
                self.errors.append(NCReportedError(self.source_name, _ln, "Trailing/unparsed content"))
        return stmts

    def _add_err(self, ln: int, msg: str):
        self.errors.append(NCReportedError(self.source_name, ln, msg))

    def _sync_to_indent_leq(self, i: int, indent: int) -> int:
        while i < len(self.lines):
            ind, _s, _ln = self.lines[i]
            if ind <= indent:
                break
            i += 1
        return i

    def _parse_block(self, i: int, indent: int) -> Tuple[List[Stmt], int]:
        out: List[Stmt] = []
        while i < len(self.lines):
            ind, s, ln = self.lines[i]
            if ind < indent:
                break
            if ind > indent:
                self._add_err(ln, "Unexpected indent")
                i = self._sync_to_indent_leq(i, indent)
                continue

            try:
                if (s.startswith("if ") or s.startswith("when ")) and s.endswith(":"):
                    stmt, i = self._parse_if(i, indent)
                    out.append(stmt)
                    continue
                if s.startswith("if ") or s.startswith("when "):
                    raise SyntaxError("if/when block is missing ':' at the end")

                if s.startswith("repeat ") and s.endswith(":"):
                    stmt, i = self._parse_repeat(i, indent)
                    out.append(stmt)
                    continue
                if re.match(r"repeat\s+--all\s+\(.+\)\s+times$", s) or re.match(r"repeat\s+--all\s+.+\s+times$", s):
                    out.append(self._parse_simple_stmt(s, ln))
                    i += 1
                    continue
                # single-line repeat-call syntax must be handled before the generic
                # "repeat ..." block error guard, otherwise valid code like
                # "repeat (ping) 3 times" would be misread as a broken repeat-block.
                if re.match(r"repeat\s+\((.+)\)\s+(.+)\s+times$", s):
                    out.append(self._parse_simple_stmt(s, ln))
                    i += 1
                    continue
                if re.match(r"repeat\s+([A-Za-z_][\w\.]*)\s+(.+)\s+times$", s):
                    out.append(self._parse_simple_stmt(s, ln))
                    i += 1
                    continue
                if s.startswith("repeat "):
                    raise SyntaxError("repeat block is missing ':' at the end")

                if s.startswith("while ") and s.endswith(":"):
                    stmt, i = self._parse_while(i, indent)
                    out.append(stmt)
                    continue
                if s.startswith("while "):
                    raise SyntaxError("while block is missing ':' at the end")

                if (s.startswith("for ") or s.startswith("foreach ")) and s.endswith(":"):
                    stmt, i = self._parse_for(i, indent)
                    out.append(stmt)
                    continue
                if s.startswith("for ") or s.startswith("foreach "):
                    raise SyntaxError("for/foreach block is missing ':' at the end")

                if s.startswith("fn ") and s.endswith(":"):
                    stmt, i = self._parse_fn(i, indent)
                    out.append(stmt)
                    continue
                if s.startswith("fn "):
                    raise SyntaxError("fn block is missing ':' at the end")

                inline = self._parse_inline_row(s, ln)
                if inline is not None:
                    out.append(inline)
                    i += 1
                    continue

                if re.match(r"(?:button|botton|knopf)\s+.+:$", s):
                    stmt, i = self._parse_button(i, indent)
                    out.append(stmt)
                    continue
                if re.match(r"(?:button|botton|knopf)\s+.+$", s):
                    raise SyntaxError("button block is missing ':' at the end")

                if re.match(r"\([A-Za-z_][\w]*\)\s*=\s*(?:checkmark|checkbox|check|haken|haekchen|häckchen)\s+.+$", s):
                    out.append(self._parse_simple_stmt(s, ln))
                    i += 1
                    continue

                if s.startswith("table ") and s.endswith(":"):
                    stmt, i = self._parse_table(i, indent)
                    out.append(stmt)
                    continue

                if s.startswith("world ") and s.endswith(":"):
                    stmt, i = self._parse_world(i, indent)
                    out.append(stmt)
                    continue
                if s.startswith("world "):
                    raise SyntaxError("world block is missing ':' at the end")

                if s == "agent:":
                    stmt, i = self._parse_agent(i, indent)
                    out.append(stmt)
                    continue
                if s == "agent":
                    raise SyntaxError("agent block is missing ':' at the end")

                if s.startswith("pick ") and s.endswith(":"):
                    stmt, i = self._parse_pick(i, indent)
                    out.append(stmt)
                    continue
                if s.startswith("pick "):
                    raise SyntaxError("pick block is missing ':' at the end")

                if s.startswith("text ") and s.endswith(":"):
                    stmt, i = self._parse_textstyle(i, indent)
                    out.append(stmt)
                    continue
                if s.startswith("anim ") and s.endswith(":"):
                    stmt, i = self._parse_anim(i, indent)
                    out.append(stmt)
                    continue

                if s.startswith("elif "):
                    raise SyntaxError("elif without matching if")
                if s == "else:" or s == "else":
                    raise SyntaxError("else without matching if")

                out.append(self._parse_simple_stmt(s, ln))
                i += 1

            except Exception as e:
                self._add_err(ln, str(e))
                i += 1
                continue
        return out, i

    def _parse_inline_row(self, s: str, ln: int) -> Optional[Stmt]:
        """Parse explicit same-line output/control rows.

        The syntax deliberately uses NC's normal word-based calls rather than
        Python-style parentheses, for example::

            button "A" button "B"
            print "Hello" print name
            (sound) = checkmark "Sound" (music) = checkmark "Music"
            input "Name" input "City"

        A single command is left to the existing statement parsers so old NC
        programs keep their exact behavior.
        """
        commands = _split_inline_commands(s)
        if len(commands) < 2:
            return None

        items: List[Dict[str, Any]] = []
        checkmark_kinds = {"checkmark", "checkbox", "check", "haken", "haekchen", "häckchen"}
        button_kinds = {"button", "botton", "knopf"}
        for index, (keyword, segment) in enumerate(commands):
            if keyword in button_kinds:
                match = re.match(r"(?:button|botton|knopf)\s+(.+)$", segment)
                if not match or not match.group(1).strip():
                    raise SyntaxError("Inline button needs a label, for example: button \"A\"")
                items.append({"kind": "button", "label": match.group(1).strip(), "color": None})
                continue

            if keyword in checkmark_kinds:
                pattern = r"(?:\(([A-Za-z_]\w*)\)\s*=\s*)?(?:" + re.escape(keyword) + r")\s+(.+?)\s+color\s+(.+)$"
                match = re.match(pattern, segment)
                if match:
                    name, label, color = match.group(1), match.group(2), match.group(3)
                else:
                    pattern = r"(?:\(([A-Za-z_]\w*)\)\s*=\s*)?(?:" + re.escape(keyword) + r")\s+(.+)$"
                    match = re.match(pattern, segment)
                    if not match:
                        raise SyntaxError("Inline checkmark needs a label, for example: checkmark \"Sound\"")
                    name, label, color = match.group(1), match.group(2), None
                if not name:
                    name = f"__inline_checkmark_{ln}_{index}"
                items.append({"kind": "checkmark", "name": name, "label": label.strip(), "color": color.strip() if color else None})
                continue

            if keyword == "print":
                match = re.match(r"print\s+(.+)$", segment)
                if not match:
                    raise SyntaxError("Inline print needs a value")
                items.append({"kind": "print", "args": _split_commas(match.group(1))})
                continue

            if keyword == "input":
                match = re.match(r"(?:\(([A-Za-z_]\w*)\)\s*=\s*)?input\s+(.+)$", segment)
                if not match:
                    raise SyntaxError("Inline input needs a prompt, for example: input \"Name\"")
                items.append({"kind": "input", "name": match.group(1), "prompt": match.group(2).strip()})
                continue

            raise SyntaxError(f"Unsupported inline command: {keyword}")

        return Stmt("inline_row", {"items": items}, ln)

    def _parse_simple_stmt(self, s: str, ln: int) -> Stmt:
        m = re.match(r"export\s+([A-Za-z_]\w*)$", s)
        if m:
            return Stmt("export", {"name": m.group(1)}, ln)

        m = re.match(r"export\s+--all$", s)
        if m:
            return Stmt("export_all", {}, ln)

        m = re.match(r"import\s+([A-Za-z_]\w*)$", s)
        if m:
            return Stmt("import", {"name": m.group(1)}, ln)

        m = re.match(r'from\s+"([^"]+)"\s+import\s+([A-Za-z_]\w*)(?:\s+as\s+([A-Za-z_]\w*))?$', s)
        if m:
            return Stmt("from_import", {"base": m.group(1), "name": m.group(2), "as": m.group(3)}, ln)

        m = re.match(r"let\s+([A-Za-z_]\w*)\s*=\s*(.+)$", s)
        if m:
            return Stmt("let", {"name": m.group(1), "expr": m.group(2)}, ln)

        m = re.match(r"set\s+([A-Za-z_]\w*)\s*=\s*(.+)$", s)
        if m:
            return Stmt("set", {"name": m.group(1), "expr": m.group(2)}, ln)

        if s == "ret" or s == "return":
            return Stmt("return", {"expr": "None"}, ln)

        m = re.match(r"(ret|return)\s+(.+)$", s)
        if m:
            return Stmt("return", {"expr": m.group(2)}, ln)

        if s == "break":
            return Stmt("break", {}, ln)
        if s == "continue":
            return Stmt("continue", {}, ln)
        if s == "end":
            return Stmt("end", {}, ln)

        m = re.match(r"repeat\s+--all\s+\((.+)\)\s+times$", s)
        if m:
            return Stmt("repeat_program", {"n": m.group(1).strip()}, ln)

        m = re.match(r"repeat\s+--all\s+(.+)\s+times$", s)
        if m:
            return Stmt("repeat_program", {"n": m.group(1).strip()}, ln)

        m = re.match(r"(?:textcolor|textcollor|textcolour|fontcolor|printcolor)\s+--all\s+(.+)$", s)
        if m:
            return Stmt("text_color_all", {"expr": m.group(1).strip()}, ln)

        m = re.match(r"(?:textcolor|textcollor|textcolour|fontcolor|printcolor)\s+(.+)$", s)
        if m:
            return Stmt("text_color", {"expr": m.group(1).strip()}, ln)

        m = re.match(r"repeat\s+\((.+)\)\s+(.+)\s+times$", s)
        if m:
            return Stmt("repeat_call", {"action": m.group(1).strip(), "n": m.group(2).strip()}, ln)

        m = re.match(r"repeat\s+([A-Za-z_][\w\.]*)\s+(.+)\s+times$", s)
        if m:
            return Stmt("repeat_call", {"action": m.group(1).strip(), "n": m.group(2).strip()}, ln)

        m = re.match(r"print\s+(.+)$", s)
        if m:
            return Stmt("print", {"args": _split_commas(m.group(1))}, ln)

        m = re.match(r"\(([A-Za-z_][\w]*)\)\s*=\s*(?:checkmark|checkbox|check|haken|haekchen|häckchen)\s+(.+?)\s+color\s+(.+)$", s)
        if m:
            return Stmt("checkmark", {"name": m.group(1), "label": m.group(2).strip(), "color": m.group(3).strip()}, ln)

        m = re.match(r"\(([A-Za-z_][\w]*)\)\s*=\s*(?:checkmark|checkbox|check|haken|haekchen|häckchen)\s+(.+)$", s)
        if m:
            return Stmt("checkmark", {"name": m.group(1), "label": m.group(2).strip(), "color": None}, ln)

        m = re.match(r"color\s+--all\s+(.+)$", s)
        if m:
            return Stmt("button_color_all", {"expr": m.group(1).strip()}, ln)

        m = re.match(r"color\s+(.+)$", s)
        if m:
            return Stmt("button_color", {"expr": m.group(1).strip()}, ln)

        if s == "action:" or s == "action":
            raise SyntaxError("action block is only allowed inside a button")

        m = re.match(r'window\s+"([^"]+)"\s+size\s+(\d+)\s+(\d+)(?:\s+id\s+([A-Za-z_]\w*))?$', s)
        if m:
            return Stmt("ui_window", {"title": m.group(1), "w": int(m.group(2)), "h": int(m.group(3)), "id": m.group(4)}, ln)

        m = re.match(r'plot\s+"([^"]+)"\s+(.+)$', s)
        if m:
            return Stmt("ui_plot", {"series": m.group(1), "expr": m.group(2)}, ln)

        m = re.match(r'table\s+"([^"]+)"\s+\[(.+)\]\s*$', s)
        if m:
            vars_ = [x.strip() for x in _split_commas(m.group(2))]
            return Stmt("ui_table", {"name": m.group(1), "vars": vars_}, ln)

        if s == "tick":
            return Stmt("ui_tick", {}, ln)


        # --- UI mini DSL (inside pick) ---
        m = re.match(r'text\s+(.+)$', s)
        if m and (not s.endswith(":")):
            return Stmt("pick_text", {"expr": m.group(1)}, ln)

        m = re.match(r'html\s+(.+)$', s)
        if m and (not s.endswith(":")):
            return Stmt("pick_html", {"expr": m.group(1)}, ln)

        m = re.match(r'css\s+(.+)$', s)
        if m and (not s.endswith(":")):
            return Stmt("pick_css", {"expr": m.group(1)}, ln)

        m = re.match(r'use\s+([A-Za-z_]\w*)$', s)
        if m:
            return Stmt("pick_use", {"name": m.group(1)}, ln)

        m = re.match(r'anim\s+([A-Za-z_]\w*)\s+(.+)$', s)
        if m and (not s.endswith(":")):
            return Stmt("pick_anim", {"name": m.group(1), "spec": m.group(2)}, ln)

        # --- Execute another NC file (include/run) ---
        m = re.match(r'run\s+"([^"]+)"\s*$', s)
        if m:
            return Stmt("run_file", {"target": m.group(1)}, ln)
        m = re.match(r"run\s+([^\s]+)\s*$", s)
        if m:
            return Stmt("run_file", {"target": m.group(1)}, ln)

        # --- Render current UI doc ---
        m = re.match(r'ren(?:\s+"([^"]+)")?(?:\s+size\s+(\d+)\s+(\d+))?\s*$', s)
        if m:
            return Stmt("render", {"title": m.group(1), "w": m.group(2), "h": m.group(3)}, ln)

        # Plain assignment is the concise update form. ``let`` and ``set``
        # remain available and retain their existing semantics; accepting
        # ``name = value`` is especially useful in button preparation blocks.
        m = re.match(r"([A-Za-z_]\w*)\s*=\s*(.+)$", s)
        if m:
            return Stmt("set", {"name": m.group(1), "expr": m.group(2).strip()}, ln)

        return Stmt("expr", {"expr": s}, ln)

    def _parse_if(self, i: int, indent: int) -> Tuple[Stmt, int]:
        _ind, s, ln = self.lines[i]
        prefix = "when " if s.startswith("when ") else "if "
        cond = s[len(prefix):-1].strip()
        i += 1
        then_block, i = self._parse_block(i, indent + 1)

        elifs = []
        else_block = None

        while i < len(self.lines):
            ind2, s2, ln2 = self.lines[i]
            if ind2 != indent:
                break
            if s2.startswith("elif ") and s2.endswith(":"):
                c = s2[len("elif "):-1].strip()
                i += 1
                b, i = self._parse_block(i, indent + 1)
                elifs.append((c, b, ln2))
                continue
            if s2 == "else:":
                i += 1
                else_block, i = self._parse_block(i, indent + 1)
                break
            break

        return Stmt("if", {"cond": cond, "then": then_block, "elifs": elifs, "else": else_block}, ln), i

    def _parse_repeat(self, i: int, indent: int) -> Tuple[Stmt, int]:
        _ind, s, ln = self.lines[i]
        n_expr = s[len("repeat "):-1].strip()
        i += 1
        body, i = self._parse_block(i, indent + 1)
        return Stmt("repeat", {"n": n_expr, "body": body}, ln), i

    def _parse_while(self, i: int, indent: int) -> Tuple[Stmt, int]:
        _ind, s, ln = self.lines[i]
        cond = s[len("while "):-1].strip()
        i += 1
        body, i = self._parse_block(i, indent + 1)
        return Stmt("while", {"cond": cond, "body": body}, ln), i

    def _parse_for(self, i: int, indent: int) -> Tuple[Stmt, int]:
        _ind, s, ln = self.lines[i]
        m = re.match(r"(?:for|foreach)\s+([A-Za-z_]\w*)\s+in\s+(.+):$", s)
        if not m:
            raise SyntaxError("Bad for statement")
        var = m.group(1)
        expr = m.group(2).strip()
        i += 1
        body, i = self._parse_block(i, indent + 1)
        return Stmt("for", {"var": var, "iter": expr, "body": body}, ln), i

    def _parse_fn(self, i: int, indent: int) -> Tuple[Stmt, int]:
        _ind, s, ln = self.lines[i]
        m = re.match(r"fn\s+([A-Za-z_]\w*)\((.*?)\):$", s)
        if not m:
            raise SyntaxError("Bad fn statement")
        name = m.group(1)
        args = [a.strip() for a in m.group(2).split(",") if a.strip()]
        i += 1
        body, i = self._parse_block(i, indent + 1)
        return Stmt("fn", {"name": name, "args": args, "body": body}, ln), i

    def _parse_button(self, i: int, indent: int) -> Tuple[Stmt, int]:
        _ind, s, ln = self.lines[i]
        m = re.match(r"(?:button|botton|knopf)\s+(.+):$", s)
        if not m:
            raise SyntaxError("Bad button statement")
        label_expr = m.group(1).strip()
        i += 1
        color_expr = None
        action_body = None
        preparation_body: List[Stmt] = []
        action_seen = False

        while i < len(self.lines):
            ind2, s2, ln2 = self.lines[i]
            if ind2 < indent + 1:
                break
            if ind2 > indent + 1:
                self._add_err(ln2, "Unexpected indent in button")
                i += 1
                continue

            m_color = re.match(r"color\s+(.+)$", s2)
            if m_color:
                if action_seen:
                    raise SyntaxError("button color and preparation statements must be before 'action:'")
                color_expr = m_color.group(1).strip()
                i += 1
                continue

            if s2 == "action:" or s2 == "action":
                if not s2.endswith(":"):
                    raise SyntaxError("action block is missing ':' at the end")
                i += 1
                action_body, i = self._parse_block(i, indent + 2)
                action_seen = True
                continue

            if action_seen:
                raise SyntaxError("Only one action block is allowed inside a button")

            # These statements run only after this button is selected,
            # immediately before its action body. This keeps button-specific
            # setup local to the selected action while allowing several lines
            # of let/set/plain-assignment preparation.
            preparation = self._parse_simple_stmt(s2, ln2)
            if preparation.kind not in {"let", "set"}:
                raise SyntaxError("Only 'color ...', variable assignments, and 'action:' are allowed inside a button")
            preparation_body.append(preparation)
            i += 1

        if action_body is None:
            raise SyntaxError("button is missing an action block")

        return Stmt(
            "button",
            {
                "label": label_expr,
                "color": color_expr,
                "preparation": preparation_body,
                "action": action_body,
            },
            ln,
        ), i

    def _parse_table(self, i: int, indent: int) -> Tuple[Stmt, int]:
        _ind, s, ln = self.lines[i]
        m = re.match(r"table\s+([A-Za-z_]\w*):$", s)
        if not m:
            raise SyntaxError("Bad table statement")
        name = m.group(1)
        i += 1
        rows = []
        while i < len(self.lines):
            ind2, s2, ln2 = self.lines[i]
            if ind2 < indent + 1:
                break
            if ind2 > indent + 1:
                self._add_err(ln2, "Unexpected indent in table")
                i = self._sync_to_indent_leq(i, indent + 1)
                continue
            try:
                if "=" in s2:
                    k, ex = [x.strip() for x in s2.split("=", 1)]
                else:
                    parts = s2.split(None, 1)
                    if len(parts) != 2:
                        raise SyntaxError("Bad table row")
                    k, ex = parts[0], parts[1]
                rows.append((k, ex, ln2))
            except Exception as e:
                self._add_err(ln2, str(e))
            i += 1
        return Stmt("table", {"name": name, "rows": rows}, ln), i

    def _parse_world(self, i: int, indent: int) -> Tuple[Stmt, int]:
        _ind, s, ln = self.lines[i]
        m = re.match(r"world\s+([A-Za-z_]\w*):$", s)
        if not m:
            raise SyntaxError("Bad world statement")
        name = m.group(1)
        i += 1

        state_init: Dict[str, float] = {}
        actions: List[str] = []
        bounds: Dict[str, Tuple[float, float]] = {}
        step_body: List[Stmt] = []
        in_step = False

        while i < len(self.lines):
            ind2, s2, ln2 = self.lines[i]
            if ind2 < indent + 1:
                break

            if ind2 > indent + 1 and not in_step:
                self._add_err(ln2, "Unexpected indent in world")
                i = self._sync_to_indent_leq(i, indent + 1)
                continue

            if ind2 == indent + 1:
                try:
                    if s2.startswith("state "):
                        rest = s2[len("state "):].strip()
                        parts = _split_commas(rest)
                        for p in parts:
                            if "=" not in p:
                                raise SyntaxError("Bad state assignment")
                            k, ex = [x.strip() for x in p.split("=", 1)]
                            state_init[k] = float(ex)
                        i += 1
                        continue

                    if s2.startswith("actions "):
                        m2 = re.match(r"actions\s+\[(.+)\]$", s2)
                        if not m2:
                            raise SyntaxError("Bad actions list")
                        actions = [x.strip() for x in m2.group(1).split(",") if x.strip()]
                        i += 1
                        continue

                    if s2.startswith("bounds "):
                        m2 = re.match(r"bounds\s+([A-Za-z_]\w*)\s+([0-9.\-]+)\.\.([0-9.\-]+)$", s2)
                        if not m2:
                            raise SyntaxError("Bad bounds")
                        var = m2.group(1)
                        lo = float(m2.group(2))
                        hi = float(m2.group(3))
                        bounds[var] = (lo, hi)
                        i += 1
                        continue

                    if s2 == "step:":
                        in_step = True
                        i += 1
                        step_body, i = self._parse_block(i, indent + 2)
                        in_step = False
                        continue

                    self._add_err(ln2, f"Unknown world directive: {s2}")
                    i += 1
                    continue

                except Exception as e:
                    self._add_err(ln2, str(e))
                    i += 1
                    continue

            i += 1

        if not actions:
            self._add_err(ln, f"World '{name}' missing actions")
        if not step_body:
            self._add_err(ln, f"World '{name}' missing step block")

        return Stmt("world", {"name": name, "state": state_init, "actions": actions, "bounds": bounds, "step": step_body}, ln), i

    def _parse_agent(self, i: int, indent: int) -> Tuple[Stmt, int]:
        _ind, s, ln = self.lines[i]
        if s != "agent:":
            raise SyntaxError("Bad agent statement")
        i += 1
        body, i = self._parse_block(i, indent + 1)
        return Stmt("agent", {"body": body}, ln), i



    def _parse_pick(self, i: int, indent: int) -> Tuple[Stmt, int]:
        _ind, s, ln = self.lines[i]
        # pick "<selector>":
        rest = s[len("pick "):].rstrip(":").strip()
        if not rest:
            raise SyntaxError("pick: missing selector")
        i += 1
        body, i = self._parse_block(i, indent + 1)
        return Stmt("pick", {"selector": rest, "body": body}, ln), i

    def _parse_textstyle(self, i: int, indent: int) -> Tuple[Stmt, int]:
        _ind, s, ln = self.lines[i]
        # text name:
        name = s[len("text "):].rstrip(":").strip()
        if not re.fullmatch(r"[A-Za-z_]\w*", name or ""):
            raise SyntaxError("text: bad name (use letters/numbers/_)")
        i += 1
        decls: List[str] = []
        while i < len(self.lines):
            ind2, s2, ln2 = self.lines[i]
            if ind2 < indent + 1:
                break
            if ind2 > indent + 1:
                self._add_err(ln2, "Unexpected indent in text")
                i = self._sync_to_indent_leq(i, indent + 1)
                continue
            # keep raw CSS declaration lines
            decls.append(s2.strip().rstrip(";"))
            i += 1
        return Stmt("textstyle", {"name": name, "decls": ";".join([d for d in decls if d])}, ln), i

    def _parse_anim(self, i: int, indent: int) -> Tuple[Stmt, int]:
        _ind, s, ln = self.lines[i]
        # anim name:
        name = s[len("anim "):].rstrip(":").strip()
        if not re.fullmatch(r"[A-Za-z_]\w*", name or ""):
            raise SyntaxError("anim: bad name (use letters/numbers/_)")
        i += 1
        lines: List[str] = []
        while i < len(self.lines):
            ind2, s2, ln2 = self.lines[i]
            if ind2 < indent + 1:
                break
            if ind2 > indent + 1:
                self._add_err(ln2, "Unexpected indent in anim")
                i = self._sync_to_indent_leq(i, indent + 1)
                continue
            lines.append(s2.strip())
            i += 1
        return Stmt("anim", {"name": name, "body": " ".join(lines)}, ln), i


# ============================================================
# Interpreter
# ============================================================

def _nc_callable(fn: Callable) -> Callable:
    setattr(fn, "__nc_callable__", True)
    return fn


def clamp(x: float, lo: float, hi: float) -> float:
    return lo if x < lo else hi if x > hi else x


@_nc_callable
def clip(x: float, lo: float, hi: float) -> float:
    return clamp(float(x), float(lo), float(hi))


@_nc_callable
def mean(xs: List[float]) -> float:
    xs = list(xs)
    return sum(xs) / max(1, len(xs))


@_nc_callable
def std(xs: List[float]) -> float:
    xs = list(xs)
    if len(xs) < 2:
        return 0.0
    m = mean(xs)
    v = sum((x - m) ** 2 for x in xs) / (len(xs) - 1)
    return v ** 0.5


@_nc_callable
def sigmoid(x: float) -> float:
    x = float(x)
    if x >= 0:
        z = pow(2.718281828, -x)
        return 1.0 / (1.0 + z)
    else:
        z = pow(2.718281828, x)
        return z / (1.0 + z)


@_nc_callable
def softmax(xs: List[float]) -> List[float]:
    xs = [float(x) for x in xs]
    if not xs:
        return []
    m = max(xs)
    exps = [pow(2.718281828, x - m) for x in xs]
    s = sum(exps)
    return [e / s for e in exps]


@_nc_callable
def rand() -> float:
    return random.random()


@_nc_callable
def randint(a: int, b: int) -> int:
    return random.randint(int(a), int(b))


@_nc_callable
def time_ms() -> int:
    return int(time.time() * 1000)


# -----------------------------
# Convenience list/dict helpers (NC-friendly)
# -----------------------------

@_nc_callable
def push(xs: list, value: Any):
    xs = list(xs) if not isinstance(xs, list) else xs
    xs.append(value)
    return xs

@_nc_callable
def pop(xs: list):
    xs = list(xs) if not isinstance(xs, list) else xs
    if not xs:
        return None
    return xs.pop()

@_nc_callable
def get(map_obj: Any, key: Any, default: Any = None):
    try:
        if isinstance(map_obj, dict):
            return map_obj.get(key, default)
        if isinstance(map_obj, list) and isinstance(key, int):
            return map_obj[key] if -len(map_obj) <= key < len(map_obj) else default
    except Exception:
        pass
    return default

@_nc_callable
def put(map_obj: Any, key: Any, value: Any):
    if isinstance(map_obj, dict):
        map_obj[key] = value
        return map_obj
    if isinstance(map_obj, list) and isinstance(key, int):
        while key >= len(map_obj):
            map_obj.append(None)
        map_obj[key] = value
        return map_obj
    return map_obj

@_nc_callable
def keys(map_obj: Any):
    return list(map_obj.keys()) if isinstance(map_obj, dict) else []

@_nc_callable
def values(map_obj: Any):
    return list(map_obj.values()) if isinstance(map_obj, dict) else []

@_nc_callable
def items(map_obj: Any):
    return list(map_obj.items()) if isinstance(map_obj, dict) else []

# -----------------------------
# Built-in lightweight AI helpers
#   - Markov text model (word-level)
#   - Simple bag-of-words classifier (softmax / logistic regression)
# -----------------------------

_TOKEN_RE = re.compile(r"[A-Za-zÄÖÜäöüß0-9]+", re.UNICODE)

def _tokenize(text: str) -> List[str]:
    if text is None:
        return []
    s = str(text)
    return [t.lower() for t in _TOKEN_RE.findall(s)]

class MarkovTextModel:
    def __init__(self, order: int = 3):
        self.order = max(1, int(order))
        self.next_counts: Dict[Tuple[str, ...], Dict[str, int]] = {}
        self.total_next: Dict[Tuple[str, ...], int] = {}

    def reset(self, order: Optional[int] = None):
        if order is not None:
            self.order = max(1, int(order))
        self.next_counts.clear()
        self.total_next.clear()

    def learn(self, text: str):
        toks = _tokenize(text)
        if len(toks) <= self.order:
            return
        for i in range(len(toks) - self.order):
            key = tuple(toks[i:i + self.order])
            nxt = toks[i + self.order]
            d = self.next_counts.get(key)
            if d is None:
                d = {}
                self.next_counts[key] = d
                self.total_next[key] = 0
            d[nxt] = int(d.get(nxt, 0)) + 1
            self.total_next[key] = int(self.total_next.get(key, 0)) + 1

    def _sample_next(self, key: Tuple[str, ...]) -> Optional[str]:
        d = self.next_counts.get(key)
        if not d:
            return None
        total = self.total_next.get(key, 0)
        if total <= 0:
            return None
        r = random.randint(1, total)
        s = 0
        for tok, c in d.items():
            s += c
            if s >= r:
                return tok
        return next(iter(d.keys()), None)

    def generate(self, seed: str = "", n_tokens: int = 30) -> str:
        n_tokens = max(1, int(n_tokens))
        seed_toks = _tokenize(seed)
        out: List[str] = []

        if len(seed_toks) >= self.order:
            ctx = tuple(seed_toks[-self.order:])
            out.extend(seed_toks)
        else:
            if not self.next_counts:
                return str(seed) if seed is not None else ""
            ctx = random.choice(list(self.next_counts.keys()))
            out.extend(list(ctx))

        for _ in range(n_tokens):
            nxt = self._sample_next(ctx)
            if nxt is None:
                break
            out.append(nxt)
            ctx = tuple(out[-self.order:])
        return " ".join(out)

    def to_json(self) -> Dict[str, Any]:
        pairs = []
        for k, d in self.next_counts.items():
            pairs.append([list(k), d])
        return {"order": self.order, "pairs": pairs}

    @staticmethod
    def from_json(obj: Any) -> "MarkovTextModel":
        m = MarkovTextModel(int(obj.get("order", 3)) if isinstance(obj, dict) else 3)
        if not isinstance(obj, dict):
            return m
        for k_list, d in obj.get("pairs", []):
            try:
                key = tuple(str(x) for x in k_list)
                if isinstance(d, dict):
                    m.next_counts[key] = {str(t): int(c) for t, c in d.items()}
                    m.total_next[key] = sum(m.next_counts[key].values())
            except Exception:
                continue
        return m

class SimpleTextClassifier:
    def __init__(self, classes: int = 2, max_vocab: int = 2000):
        self.classes = max(2, int(classes))
        self.max_vocab = max(64, int(max_vocab))
        self.vocab: Dict[str, int] = {}
        self.W: List[List[float]] = []
        self.b: List[float] = []
        self.data: List[Tuple[List[str], int]] = []
        self._init_params()

    def _init_params(self):
        self.W = [[0.0] * len(self.vocab) for _ in range(self.classes)]
        self.b = [0.0] * self.classes

    def reset(self, classes: Optional[int] = None, max_vocab: Optional[int] = None):
        if classes is not None:
            self.classes = max(2, int(classes))
        if max_vocab is not None:
            self.max_vocab = max(64, int(max_vocab))
        self.vocab.clear()
        self.data.clear()
        self._init_params()

    def add(self, text: str, label: int):
        label = int(label)
        if label < 0 or label >= self.classes:
            return
        toks = _tokenize(text)
        if not toks:
            return
        for t in toks:
            if t not in self.vocab:
                if len(self.vocab) >= self.max_vocab:
                    continue
                self.vocab[t] = len(self.vocab)
                for c in range(self.classes):
                    self.W[c].append(0.0)
        self.data.append((toks, label))

    def _sparse_counts(self, toks: List[str]) -> Dict[int, float]:
        counts: Dict[int, float] = {}
        for t in toks:
            idx = self.vocab.get(t)
            if idx is None:
                continue
            counts[idx] = counts.get(idx, 0.0) + 1.0
        return counts

    def _softmax(self, scores: List[float]) -> List[float]:
        m = max(scores)
        exps = [pow(2.718281828, s - m) for s in scores]
        ssum = sum(exps)
        return [e / ssum for e in exps]

    def train(self, epochs: int = 20, lr: float = 0.1) -> Dict[str, Any]:
        epochs = max(1, int(epochs))
        lr = float(lr)
        if not self.data or not self.vocab:
            return {"ok": False, "error": "no data"}
        if any(len(w) != len(self.vocab) for w in self.W):
            self._init_params()

        for _ in range(epochs):
            random.shuffle(self.data)
            for toks, y in self.data:
                x = self._sparse_counts(toks)
                scores = []
                for c in range(self.classes):
                    s = self.b[c]
                    wc = self.W[c]
                    for i, v in x.items():
                        s += wc[i] * v
                    scores.append(s)
                probs = self._softmax(scores)
                for c in range(self.classes):
                    grad = probs[c] - (1.0 if c == y else 0.0)
                    self.b[c] -= lr * grad
                    wc = self.W[c]
                    for i, v in x.items():
                        wc[i] -= lr * grad * v

        return {"ok": True, "samples": len(self.data), "vocab": len(self.vocab), "classes": self.classes}

    def predict(self, text: str) -> Any:
        toks = _tokenize(text)
        x = self._sparse_counts(toks)
        if not self.vocab:
            return None
        scores = []
        for c in range(self.classes):
            s = self.b[c]
            wc = self.W[c]
            for i, v in x.items():
                if i < len(wc):
                    s += wc[i] * v
            scores.append(s)
        probs = self._softmax(scores)
        if self.classes == 2:
            return probs[1]
        return probs

    def to_json(self) -> Dict[str, Any]:
        return {
            "classes": self.classes,
            "max_vocab": self.max_vocab,
            "vocab": self.vocab,
            "W": self.W,
            "b": self.b,
            "data": [[toks, y] for toks, y in self.data],
        }

    @staticmethod
    def from_json(obj: Any) -> "SimpleTextClassifier":
        c = SimpleTextClassifier(classes=int(obj.get("classes", 2)), max_vocab=int(obj.get("max_vocab", 2000)))
        if not isinstance(obj, dict):
            return c
        c.vocab = {str(k): int(v) for k, v in obj.get("vocab", {}).items()}
        c.W = [[float(x) for x in row] for row in obj.get("W", [])]
        c.b = [float(x) for x in obj.get("b", [0.0] * c.classes)]
        c.data = []
        for row in obj.get("data", []):
            try:
                toks, y = row
                c.data.append(([str(t) for t in toks], int(y)))
            except Exception:
                continue
        if not c.W or len(c.W) != c.classes or any(len(w) != len(c.vocab) for w in c.W):
            c._init_params()
        if len(c.b) != c.classes:
            c.b = [0.0] * c.classes
        return c



@_nc_callable
def range_list(a: int, b: Optional[int] = None, step: int = 1):
    if b is None:
        start, stop = 0, int(a)
    else:
        start, stop = int(a), int(b)
    step = int(step)
    return list(range(start, stop, step))


# -----------------------------
# JSON (python-backed builtin module)
# -----------------------------

def _safe_json_dir(policy: NCPolicy) -> str:
    base = policy.data_dir or os.path.join(os.getcwd(), "data")
    os.makedirs(base, exist_ok=True)
    return base


def _safe_json_path(name: str, policy: NCPolicy) -> str:
    name = str(name).strip()
    if not re.fullmatch(r"[A-Za-z0-9_\-\.]{1,80}", name):
        raise RuntimeError("json.save/load: invalid name (use letters/numbers/_-.)")
    if not name.lower().endswith(".json"):
        name += ".json"

    base = _safe_json_dir(policy)
    path = os.path.abspath(os.path.join(base, name))

    base_abs = os.path.abspath(base)
    if not path.startswith(base_abs + os.sep):
        raise RuntimeError("json.save/load: blocked path")
    return path




# -----------------------------
# JSON secure store (anti-tamper + last-good fallback)
# -----------------------------

_JSON_LAST_GOOD: Dict[str, Any] = {}  # key = absolute json path -> last verified object
_JSON_SECRET_CACHE: Dict[str, bytes] = {}  # key = secret path -> secret bytes


def _json_secret_path(policy: NCPolicy) -> str:
    base = _safe_json_dir(policy)
    return os.path.join(base, "_nc_json_secret.key")


def _load_or_create_secret(policy: NCPolicy) -> bytes:
    sp = _json_secret_path(policy)
    if sp in _JSON_SECRET_CACHE:
        return _JSON_SECRET_CACHE[sp]

    key: bytes
    try:
        if os.path.isfile(sp):
            with open(sp, "rb") as f:
                key = f.read().strip()
            if len(key) < 16:
                key = secrets.token_bytes(32)
        else:
            key = secrets.token_bytes(32)
            tmp = sp + ".tmp"
            with open(tmp, "wb") as f:
                f.write(key)
            os.replace(tmp, sp)
    except Exception:
        # fallback: ephemeral key (still prevents accidental corruption, but not durable)
        key = secrets.token_bytes(32)

    _JSON_SECRET_CACHE[sp] = key
    return key


def _canonical_json_bytes(obj: Any) -> bytes:
    s = json.dumps(obj, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return s.encode("utf-8")


def _hmac_hex(secret: bytes, payload: bytes) -> str:
    return hmac.new(secret, payload, hashlib.sha256).hexdigest()


def _sig_path(json_path: str) -> str:
    return json_path + ".sig"


def _read_text_limited(path: str, limit_chars: int) -> str:
    # limit_chars is char-count; we also enforce bytes later where needed
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return f.read(limit_chars)


def _atomic_write_text(path: str, text: str) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", errors="strict") as f:
        f.write(text)
    os.replace(tmp, path)


def _acquire_lock(lock_file: str, timeout_sec: float = 2.5) -> bool:
    t0 = time.time()
    while True:
        try:
            fd = os.open(lock_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
            return True
        except FileExistsError:
            if time.time() - t0 >= timeout_sec:
                return False
            time.sleep(0.03)
        except Exception:
            return False


def _release_lock(lock_file: str) -> None:
    try:
        if os.path.isfile(lock_file):
            os.remove(lock_file)
    except Exception:
        pass


def _deep_merge(base: Any, patch: Any) -> Any:
    # Merge dict->dict recursively; lists/scalars replaced
    if isinstance(base, dict) and isinstance(patch, dict):
        out = dict(base)
        for k, v in patch.items():
            if k in out:
                out[k] = _deep_merge(out[k], v)
            else:
                out[k] = v
        return out
    return patch


def _json_verify_on_disk(json_path: str, policy: NCPolicy) -> Tuple[bool, Any]:
    """
    returns: (ok, parsed_obj_or_None)
    ok=True only if signature exists and matches.
    If signature missing but JSON parseable: returns (False, obj) so caller may seal it.
    """
    if not os.path.isfile(json_path):
        return False, None

    try:
        raw = _read_text_limited(json_path, policy.max_module_bytes + 1)
        if len(raw.encode("utf-8")) > policy.max_module_bytes:
            return False, None
        obj = json.loads(raw)
    except Exception:
        return False, None

    sigp = _sig_path(json_path)
    if not os.path.isfile(sigp):
        return False, obj

    try:
        sig = _read_text_limited(sigp, 512).strip()
    except Exception:
        return False, obj

    secret = _load_or_create_secret(policy)
    calc = _hmac_hex(secret, _canonical_json_bytes(obj))
    return (hmac.compare_digest(sig, calc), obj)


def _json_seal(json_path: str, obj: Any, policy: NCPolicy) -> None:
    secret = _load_or_create_secret(policy)
    sig = _hmac_hex(secret, _canonical_json_bytes(obj))
    _atomic_write_text(_sig_path(json_path), sig)


def _json_load_secure(name: str, policy: NCPolicy, default: Any = None) -> Any:
    path = _safe_json_path(name, policy)
    lock = path + ".lock"

    if not _acquire_lock(lock):
        return _JSON_LAST_GOOD.get(path, default if default is not None else {})

    try:
        ok, obj = _json_verify_on_disk(path, policy)

        # Missing file -> create sealed default
        if (obj is None) and (not os.path.isfile(path)):
            obj = default if default is not None else {}
            data = _json_pretty(obj)
            if len(data.encode("utf-8")) > policy.max_module_bytes:
                raise RuntimeError("json.load: default too large")
            _atomic_write_text(path, data)
            _json_seal(path, obj, policy)
            _JSON_LAST_GOOD[path] = obj
            return obj

        # Unsigned but parseable -> seal once
        if (not ok) and (obj is not None) and (not os.path.isfile(_sig_path(path))):
            _json_seal(path, obj, policy)
            _JSON_LAST_GOOD[path] = obj
            return obj

        # Signed OK
        if ok and (obj is not None):
            _JSON_LAST_GOOD[path] = obj
            return obj

        # Tampered or unreadable -> fallback to last-good / default
        if path in _JSON_LAST_GOOD:
            return _JSON_LAST_GOOD[path]
        return default if default is not None else {}

    finally:
        _release_lock(lock)


def _json_save_secure(name: str, obj: Any, policy: NCPolicy, merge: bool = True) -> Dict[str, Any]:
    path = _safe_json_path(name, policy)
    lock = path + ".lock"

    if not _acquire_lock(lock):
        return {"ok": False, "error": "json.save: locked/busy"}

    try:
        last = _JSON_LAST_GOOD.get(path)

        ok_disk, disk_obj = _json_verify_on_disk(path, policy)

        # Base = last-good preferred; if none -> use verified disk; else {}
        base = last
        if base is None:
            if ok_disk and (disk_obj is not None):
                base = disk_obj
            else:
                base = {}

        tamper_detected = False
        if os.path.isfile(path) and os.path.isfile(_sig_path(path)) and (not ok_disk):
            tamper_detected = True

        final_obj: Any = obj
        if merge and isinstance(base, dict) and isinstance(obj, dict):
            final_obj = _deep_merge(base, obj)

        data = _json_pretty(final_obj)
        if len(data.encode("utf-8")) > policy.max_module_bytes:
            return {"ok": False, "error": "json.save: blocked (too large)"}

        _atomic_write_text(path, data)
        _json_seal(path, final_obj, policy)
        _JSON_LAST_GOOD[path] = final_obj

        return {"ok": True, "path": path, "tamper_detected": tamper_detected}

    finally:
        _release_lock(lock)


def _json_verify_secure(name: str, policy: NCPolicy) -> bool:
    path = _safe_json_path(name, policy)
    ok, _ = _json_verify_on_disk(path, policy)
    return bool(ok)


def _json_last_secure(name: str, policy: NCPolicy) -> Any:
    path = _safe_json_path(name, policy)
    return _JSON_LAST_GOOD.get(path)

def _json_encode(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def _json_pretty(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2)


def _json_parse(s: Any) -> Any:
    if not isinstance(s, str):
        s = str(s)
    return json.loads(s)


def _json_diff(a: Any, b: Any, path: str = "") -> List[List[Any]]:
    out: List[List[Any]] = []

    if type(a) != type(b):
        out.append([path or "$", "type", str(type(a).__name__), str(type(b).__name__)])
        return out

    if isinstance(a, dict):
        keys = set(a.keys()) | set(b.keys())
        for k in sorted(keys, key=lambda x: str(x)):
            p = (path + "." + str(k)) if path else str(k)
            if k not in a:
                out.append([p, "added", None, b[k]])
            elif k not in b:
                out.append([p, "removed", a[k], None])
            else:
                out.extend(_json_diff(a[k], b[k], p))
        return out

    if isinstance(a, list):
        la, lb = len(a), len(b)
        if la != lb:
            out.append([path or "$", "len", la, lb])
        m = min(la, lb)
        for i in range(m):
            p = f"{path}[{i}]" if path else f"[{i}]"
            out.extend(_json_diff(a[i], b[i], p))
        return out

    if a != b:
        out.append([path or "$", "value", a, b])
    return out


def _schema_validate(obj: Any, schema: Any, path: str = "") -> List[List[Any]]:
    errors: List[List[Any]] = []

    if not isinstance(schema, dict):
        errors.append([path or "$", "schema must be object"])
        return errors

    stype = schema.get("type")
    if stype:
        ok = True
        if stype == "object":
            ok = isinstance(obj, dict)
        elif stype == "array":
            ok = isinstance(obj, list)
        elif stype == "string":
            ok = isinstance(obj, str)
        elif stype == "number":
            ok = isinstance(obj, (int, float)) and not isinstance(obj, bool)
        elif stype == "bool":
            ok = isinstance(obj, bool)
        elif stype == "null":
            ok = (obj is None)
        else:
            errors.append([path or "$", f"unknown type '{stype}'"])
            ok = True
        if not ok:
            errors.append([path or "$", f"type mismatch (need {stype})"])
            return errors

    if "enum" in schema:
        if obj not in schema["enum"]:
            errors.append([path or "$", "not in enum"])
            return errors

    if isinstance(obj, (int, float)) and not isinstance(obj, bool):
        if "min" in schema and obj < schema["min"]:
            errors.append([path or "$", f"min {schema['min']}"])
        if "max" in schema and obj > schema["max"]:
            errors.append([path or "$", f"max {schema['max']}"])

    if isinstance(obj, str):
        if "minLen" in schema and len(obj) < schema["minLen"]:
            errors.append([path or "$", f"minLen {schema['minLen']}"])
        if "maxLen" in schema and len(obj) > schema["maxLen"]:
            errors.append([path or "$", f"maxLen {schema['maxLen']}"])

    if isinstance(obj, dict):
        req = schema.get("required") or []
        for k in req:
            if k not in obj:
                p = (path + "." + str(k)) if path else str(k)
                errors.append([p, "missing required"])

        props = schema.get("props") or {}
        if isinstance(props, dict):
            for k, sub in props.items():
                if k in obj:
                    p = (path + "." + str(k)) if path else str(k)
                    errors.extend(_schema_validate(obj[k], sub, p))

    if isinstance(obj, list):
        item_schema = schema.get("items")
        if item_schema is not None:
            for i, v in enumerate(obj):
                p = f"{path}[{i}]" if path else f"[{i}]"
                errors.extend(_schema_validate(v, item_schema, p))

    return errors


def _json_to_table(obj: Any, max_rows: int = 400) -> List[List[Any]]:
    rows: List[List[Any]] = [["path", "value"]]

    def add(p: str, v: Any):
        if len(rows) >= max_rows:
            return
        rows.append([p, v])

    def walk(v: Any, p: str):
        if len(rows) >= max_rows:
            return
        if isinstance(v, dict):
            for k in sorted(v.keys(), key=lambda x: str(x)):
                walk(v[k], (p + "." + str(k)) if p else str(k))
            return
        if isinstance(v, list):
            for i, it in enumerate(v):
                walk(it, f"{p}[{i}]" if p else f"[{i}]")
            return
        add(p or "$", v)

    walk(obj, "")
    return rows


# ============================================================
# Interpreter (core)
# ============================================================

class NCInterpreter:
    def __init__(self, policy: Optional[NCPolicy] = None, enable_ui: bool = True):
        self.policy = policy or NCPolicy()
        self.ui = UIBridge(enabled=enable_ui)
        self.step_counter = 0
        self.module_cache: Dict[str, NCModule] = {}
        self._ai_state: Dict[str, Any] = {}

        self._current_source: str = "<text>"
        self._import_stack: List[str] = []

        # UI mini DSL state (pick/text/anim/ren)
        self._ui_doc = _UIDoc()
        self._pick_stack: List[dict] = []

        # current execution base dir (for fs + run)
        self._base_dir_current: str = "."

        # forward-reference helper (allow using vars/functions before they are defined later)
        self._current_block: Optional[List[Stmt]] = None
        self._current_index: int = -1
        self._resolving_names: set[str] = set()
        self._forward_scan_limit: int = 6000  # max statements to scan ahead (guard)
        # Whole-program forward lookup registry (per source file).
        # This lets button action blocks resolve names that are defined later
        # outside the current nested block.
        self._program_blocks: List[Tuple[str, List[Stmt]]] = []

        # Tracks whether execution currently runs inside an NC function.
        # Top-level stray `ret` remains lenient, while real function returns
        # must propagate back to NCFn.call instead of being swallowed.
        self._function_depth: int = 0

    def _tick_steps(self, n: int = 1):
        self.step_counter += n
        if self.step_counter > self.policy.max_steps:
            raise RuntimeError("NC blocked: execution step limit exceeded")

    def _raise_context_error(self, error: BaseException, source: str, line: int):
        """Add source context once without wrapping an existing NC diagnostic."""

        if isinstance(error, (NCError, NCMultiError)):
            raise error
        raise NCError(
            _format_source(source),
            int(line),
            str(error),
            self._import_stack,
        ) from error


    def _eval_expr(self, expr: str, env: Dict[str, Any], *args):
        """Evaluate expression with optional forward-reference fallback.

        Backward compatible call patterns:
          - _eval_expr(expr, env, self.policy)                         (legacy)
          - _eval_expr(expr, env, source_name, line)                   (preferred)
          - _eval_expr(expr, env)                                      (uses current source/line best-effort)
        """
        source_name = self._current_source
        line = int(getattr(env, "__line__", 1)) if hasattr(env, "__line__") else 1

        if len(args) == 1 and isinstance(args[0], NCPolicy):
            # legacy call: treat as normal safe eval, but still allow forward resolve using current context
            pass
        elif len(args) >= 2 and isinstance(args[0], str):
            source_name = str(args[0])
            line = int(args[1])
        elif len(args) == 1 and isinstance(args[0], str):
            source_name = str(args[0])

        try:
            return safe_eval_expr(expr, env, self.policy)
        except NCExprError as e:
            msg = str(e)
            mm = re.match(r"Unknown name:\s*([A-Za-z_]\w*)$", msg)
            if not mm:
                raise
            name = mm.group(1)
            if name in env:
                raise
            if name in self._resolving_names:
                raise

            self._resolving_names.add(name)
            try:
                if self._try_forward_resolve(name, env, source_name=source_name, line=line):
                    return safe_eval_expr(expr, env, self.policy)
            finally:
                self._resolving_names.discard(name)

            candidates = sorted(
                key
                for key in env.keys()
                if isinstance(key, str) and not key.startswith("__")
            )
            # Edit-distance matching avoids misleading suggestions that merely
            # share the first letter (for example `package` for `pritce`).
            suggestions = difflib.get_close_matches(name, candidates, n=3, cutoff=0.70)

            hint = ""
            if suggestions:
                hint = " Did you mean: " + ", ".join(suggestions) + "?"
            raise NCExprError(f"Unknown name: {name}.{hint}")

    def _try_forward_resolve(self, name: str, env: Dict[str, Any], source_name: str, line: int) -> bool:
        """Best-effort: resolve a missing name by scanning later statements.

        Bugfix:
        The old implementation only scanned the remaining statements of the
        current block. That failed for button action blocks, because the action
        runs in its own nested block and therefore could not see functions that
        are defined later at top level in the same file.

        New behavior:
        1) scan the remaining statements of the current block first
        2) if still unresolved, scan all registered blocks from the same source
           file (whole-program scan for the current file)
        """
        blk = self._current_block or []
        i0 = max(-1, int(self._current_index))

        candidates: List[Stmt] = []
        seen_stmt_ids: set[int] = set()

        def _append_stmt(st: Stmt) -> None:
            sid = id(st)
            if sid in seen_stmt_ids:
                return
            seen_stmt_ids.add(sid)
            candidates.append(st)

        # 1) current block remainder
        for st in blk[i0 + 1 : i0 + 1 + self._forward_scan_limit]:
            _append_stmt(st)

        # 2) whole-program scan for same source
        current_block_id = id(blk) if blk else None
        for src, block in self._program_blocks:
            if src != source_name:
                continue
            if current_block_id is not None and id(block) == current_block_id:
                continue
            for st in block:
                _append_stmt(st)
                if len(candidates) >= self._forward_scan_limit:
                    break
            if len(candidates) >= self._forward_scan_limit:
                break

        for st in candidates:
            try:
                if st.kind in ("let", "set") and st.data.get("name") == name:
                    val = safe_eval_expr(st.data.get("expr") or "None", env, self.policy)
                    env[name] = val
                    return True

                if st.kind == "fn" and st.data.get("name") == name:
                    fn = NCFn(
                        name=st.data["name"],
                        arg_names=st.data.get("args") or [],
                        body=st.data.get("body") or [],
                        closure=dict(env),
                        interp=self,
                        source=str(source_name),
                        line=int(st.line),
                        base_dir=str(getattr(self, "_base_dir_current", ".") or "."),
                    )
                    env[name] = fn
                    return True

                if st.kind == "table" and st.data.get("name") == name:
                    tbl = {}
                    for key, ex, _ln in st.data.get("rows") or []:
                        tbl[key] = safe_eval_expr(ex, env, self.policy)
                    env[name] = tbl
                    return True
            except Exception:
                # ignore failed forward resolves; keep scanning
                continue

        return False

    def _with_source(self, source: str, push_stack: bool = False):
        class _Ctx:
            def __init__(self, interp: "NCInterpreter", new_source: str, push_stack: bool):
                self.interp = interp
                self.new_source = new_source
                self.push_stack = push_stack
                self.prev_source = interp._current_source

            def __enter__(self):
                self.interp._current_source = self.new_source
                if self.push_stack:
                    self.interp._import_stack.append(self.new_source)

            def __exit__(self, exc_type, exc, tb):
                if self.push_stack and self.interp._import_stack:
                    self.interp._import_stack.pop()
                self.interp._current_source = self.prev_source
                return False

        return _Ctx(self, source, push_stack)

    def base_env(self) -> Dict[str, Any]:
        env: Dict[str, Any] = {}
        env["True"] = True
        env["False"] = False
        env["None"] = None

        env["clamp"] = _nc_callable(clamp)  # type: ignore
        env["clip"] = clip
        env["mean"] = mean
        env["std"] = std
        env["sigmoid"] = sigmoid
        env["softmax"] = softmax

        env["rand"] = rand
        env["randint"] = randint
        env["time_ms"] = time_ms

        env["range"] = range_list
        env["input"] = _nc_callable(lambda prompt="": input("" if prompt is None else str(prompt)))
        env["end"] = _nc_callable(lambda: (_ for _ in ()).throw(NCEnd()))
        env["int"] = _nc_callable(lambda value=0: int(str(value).strip()))
        env["to_int"] = _nc_callable(lambda value, default=None: int(str(value).strip()) if str(value).strip().lstrip("-").isdigit() else default)
        env["str"] = _nc_callable(lambda value="": "" if value is None else str(value))
        env["to_str"] = env["str"]
        env["float"] = _nc_callable(lambda value=0: float(str(value).strip().replace(",", ".")))
        env["to_float"] = _nc_callable(lambda value, default=None: (lambda s: float(s))(str(value).strip().replace(",", ".")) if re.fullmatch(r"-?\d+(?:\.\d+)?", str(value).strip().replace(",", ".")) else default)
        env["bool"] = _nc_callable(lambda value=None: _nc_to_bool(value))
        env["to_bool"] = env["bool"]
        env["round"] = _nc_callable(lambda value=0, digits=0: round(float(value), int(digits)))
        env["abs"] = _nc_callable(lambda value=0: abs(float(value)) if isinstance(value, (int, float, decimal.Decimal)) or str(value).strip().replace(",", ".").lstrip("-").replace(".", "", 1).isdigit() else abs(value))
        env["lower"] = _nc_callable(lambda value="": str(value).lower())
        env["upper"] = _nc_callable(lambda value="": str(value).upper())
        env["strip"] = _nc_callable(lambda value="": str(value).strip())
        env["len"] = _nc_callable(lambda value: len(value) if value is not None else 0)

        env["ui"] = self._ui_module_object()
        env["math"] = self._math_module_object()
        env["json"] = self._json_module_object()
        env["fs"] = self._fs_module_object()
        env["cam"] = self._cam_module_object()
        env["file"] = _nc_ext_file_module_object(self)
        env["time"] = _nc_ext_time_module_object(self)
        env["text"] = _nc_ext_text_module_object(self)
        env["array"] = _nc_ext_array_module_object(self)
        env["net"] = self._net_module_object()
        env["game"] = self._game_module_object()
        env["sound"] = self._sound_module_object()

        env["push"] = push
        env["pop"] = pop
        env["number"] = _nc_callable(lambda value=0, default=None: (lambda s: (float(s) if "." in s else int(s)) if re.fullmatch(r"-?\d+(?:\.\d+)?", s) else default)(str(value).strip().replace(",", ".")))
        env["text_value"] = _nc_callable(lambda value="": "" if value is None else str(value))
        env["bool_value"] = _nc_callable(lambda value=None: _nc_to_bool(value))
        env["color"] = _nc_callable(lambda value="": str(value))
        env["get"] = get
        env["put"] = put
        env["keys"] = _nc_callable(_nc_ext_keys)
        env["values"] = _nc_callable(_nc_ext_values)
        env["items"] = items
        env["min"] = _nc_callable(_nc_ext_min)
        env["max"] = _nc_callable(_nc_ext_max)
        env["contains"] = _nc_callable(_nc_ext_contains)
        env["startswith"] = _nc_callable(_nc_ext_startswith)
        env["endswith"] = _nc_callable(_nc_ext_endswith)
        env["replace"] = _nc_callable(_nc_ext_replace)
        env["split"] = _nc_callable(_nc_ext_split)
        env["join"] = _nc_callable(_nc_ext_join)
        env["typeof"] = _nc_callable(_nc_ext_typeof)
        env["avg"] = _nc_callable(_nc_ext_avg)
        env["percent"] = _nc_callable(_nc_ext_percent)
        env["check_set"] = _nc_callable(lambda name, value: _nc_ext_check_set(self, name, value))
        env["check_get"] = _nc_callable(lambda name, default=False: _nc_ext_check_get(self, name, default))
        env["check_toggle"] = _nc_callable(lambda name: _nc_ext_check_toggle(self, name))
        env["check_delete"] = _nc_callable(lambda name: _nc_ext_check_delete(self, name))

        env["ai"] = self._ai_module_object()
        env["ml"] = env["ai"]
        env["llm"] = env["ai"]

        env["alias_keywords"] = _nc_callable(lambda: sorted(_NC_ALIASABLE_KEYWORDS))
        env["alias_operators"] = _nc_callable(lambda: sorted([k for k in _NC_ALIASABLE_KEYWORDS if any(ch in k for ch in "=!<>" ) or k in {"is", "is not", "in", "not in", "and", "or", "not"}]))
        env["on"] = True
        env["off"] = False
        env["true"] = True
        env["false"] = False
        env["__text_color__"] = None
        env["__button_color_all__"] = None
        env["__worlds__"] = {}
        return env

    # -------------------------
    # builtin module objects
    # -------------------------
    def _ui_module_object(self):
        class U:
            pass

        u = U()

        @_nc_callable
        def window(title: str, w: int, h: int):
            self.ui.window(str(title), int(w), int(h))

        @_nc_callable
        def plot(series: str, value: float, step: int = 0):
            self.ui.plot(str(series), float(value), int(step))

        @_nc_callable
        def table(name: str, rows: list):
            self.ui.table(str(name), rows)

        @_nc_callable
        def html_set(html: str):
            # show HTML in NCW's HTML tab (via TwinWindow)
            self.ui.html_set(str(html))

        @_nc_callable
        def app(title: str = "NC App"):
            # ui.app is a direct event-driven API. PySide6 is loaded lazily by
            # app.run(), so headless NC programs can still import/use `ui`.
            from nc_ui_app import create_app

            return create_app(
                str(title),
                base_dir=str(self._base_dir_current or "."),
                interpreter=self,
            )

        setattr(u, "window", window)
        setattr(u, "plot", plot)
        setattr(u, "table", table)
        @_nc_callable
        def scene_set(scene):
            # render NCUI2 scene (no HTML/CSS)
            if isinstance(scene, dict):
                self.ui.scene_set(scene)
            else:
                self.ui.scene_set({"nodes": []})

        setattr(u, "html_set", html_set)
        setattr(u, "scene_set", scene_set)
        setattr(u, "app", app)
        return u

    def _math_module_object(self):
        class M:
            pass

        m = M()

        @_nc_callable
        def clamp_(x, lo, hi):
            return clamp(float(x), float(lo), float(hi))

        @_nc_callable
        def clip_(x, lo, hi):
            return clip(float(x), float(lo), float(hi))

        @_nc_callable
        def abs_(x):
            return abs(float(x))

        @_nc_callable
        def floor_(x):
            return int(_pymath.floor(float(x)))

        @_nc_callable
        def ceil_(x):
            return int(_pymath.ceil(float(x)))

        @_nc_callable
        def sqrt_(x):
            return float(_pymath.sqrt(float(x)))

        @_nc_callable
        def sin_(x):
            return float(_pymath.sin(float(x)))

        @_nc_callable
        def cos_(x):
            return float(_pymath.cos(float(x)))

        @_nc_callable
        def tan_(x):
            return float(_pymath.tan(float(x)))

        @_nc_callable
        def atan2_(y, x):
            return float(_pymath.atan2(float(y), float(x)))

        @_nc_callable
        def exp_(x):
            return float(_pymath.exp(float(x)))

        @_nc_callable
        def log_(x):
            return float(_pymath.log(float(x)))

        @_nc_callable
        def pow_(a, b):
            return float(_pymath.pow(float(a), float(b)))

        @_nc_callable
        def lerp_(a, b, t):
            tt = clamp(float(t), 0.0, 1.0)
            return float(a) + (float(b) - float(a)) * tt

        @_nc_callable
        def mean_(xs):
            return mean(list(xs))

        @_nc_callable
        def median_(xs):
            xs = [float(x) for x in list(xs)]
            if not xs:
                return 0.0
            xs.sort()
            n = len(xs)
            mid = n // 2
            if n % 2 == 1:
                return xs[mid]
            return 0.5 * (xs[mid - 1] + xs[mid])

        @_nc_callable
        def var_(xs):
            xs = [float(x) for x in list(xs)]
            n = len(xs)
            if n < 2:
                return 0.0
            mu = sum(xs) / n
            return sum((x - mu) ** 2 for x in xs) / (n - 1)

        @_nc_callable
        def std_(xs):
            return float(_pymath.sqrt(var_(xs)))

        @_nc_callable
        def dot_(a, b):
            a = list(a)
            b = list(b)
            n = min(len(a), len(b))
            s = 0.0
            for i in range(n):
                s += float(a[i]) * float(b[i])
            return s

        @_nc_callable
        def dist2_(ax, ay, bx, by):
            dx = float(ax) - float(bx)
            dy = float(ay) - float(by)
            return dx * dx + dy * dy

        @_nc_callable
        def dist_(ax, ay, bx, by):
            return float(_pymath.sqrt(dist2_(ax, ay, bx, by)))

        @_nc_callable
        def sigmoid_(x):
            return sigmoid(float(x))

        @_nc_callable
        def softmax_(xs):
            return softmax(list(xs))


        @_nc_callable
        def sign_(x):
            x = float(x)
            return -1 if x < 0 else 1 if x > 0 else 0

        @_nc_callable
        def fract_(x):
            x = float(x)
            return x - float(_pymath.floor(x))

        @_nc_callable
        def mod_(x, m):
            x = float(x)
            m = float(m)
            if m == 0.0:
                return None
            return x % m

        @_nc_callable
        def wrap_(x, lo, hi):
            x = float(x); lo = float(lo); hi = float(hi)
            if lo == hi:
                return lo
            if lo > hi:
                lo, hi = hi, lo
            r = hi - lo
            return lo + ((x - lo) % r)

        @_nc_callable
        def inv_lerp_(a, b, x):
            a = float(a); b = float(b); x = float(x)
            d = (b - a)
            if d == 0.0:
                return None
            return (x - a) / d

        @_nc_callable
        def map_range_(x, in0, in1, out0, out1):
            t = inv_lerp_(in0, in1, x)
            if t is None:
                return None
            return float(out0) + (float(out1) - float(out0)) * float(t)

        @_nc_callable
        def isclose_(a, b, rel_tol, abs_tol):
            a = float(a); b = float(b)
            rel_tol = float(rel_tol); abs_tol = float(abs_tol)
            return abs(a - b) <= max(rel_tol * max(abs(a), abs(b)), abs_tol)

        @_nc_callable
        def deg2rad_(deg):
            return float(deg) * _pymath.pi / 180.0

        @_nc_callable
        def rad2deg_(rad):
            return float(rad) * 180.0 / _pymath.pi

        @_nc_callable
        def sin_deg_(deg):
            return float(_pymath.sin(deg2rad_(deg)))

        @_nc_callable
        def cos_deg_(deg):
            return float(_pymath.cos(deg2rad_(deg)))

        @_nc_callable
        def tan_deg_(deg):
            return float(_pymath.tan(deg2rad_(deg)))

        @_nc_callable
        def normalize_angle_rad_(a):
            a = float(a)
            return wrap_(a + _pymath.pi, 0.0, 2.0 * _pymath.pi) - _pymath.pi

        @_nc_callable
        def normalize_angle_deg_(a):
            a = float(a)
            return wrap_(a + 180.0, 0.0, 360.0) - 180.0

        @_nc_callable
        def softplus_(x):
            x = float(x)
            if x > 20.0:
                return x
            if x < -20.0:
                return 0.0
            return float(_pymath.log(1.0 + _pymath.exp(x)))

        @_nc_callable
        def smoothstep_(edge0, edge1, x):
            t = inv_lerp_(edge0, edge1, x)
            if t is None:
                return None
            t = clamp(float(t), 0.0, 1.0)
            return t * t * (3.0 - 2.0 * t)

        @_nc_callable
        def smootherstep_(edge0, edge1, x):
            t = inv_lerp_(edge0, edge1, x)
            if t is None:
                return None
            t = clamp(float(t), 0.0, 1.0)
            return t*t*t*(t*(t*6.0 - 15.0) + 10.0)

        @_nc_callable
        def gauss_(x, mu, sigma):
            x = float(x); mu = float(mu); sigma = float(sigma)
            if sigma == 0.0:
                return None
            z = (x - mu) / sigma
            return float(_pymath.exp(-0.5 * z * z) / (abs(sigma) * _pymath.sqrt(2.0 * _pymath.pi)))

        @_nc_callable
        def gcd_(a, b):
            return int(_pymath.gcd(int(a), int(b)))

        @_nc_callable
        def lcm_(a, b):
            a = int(a); b = int(b)
            if a == 0 or b == 0:
                return 0
            return abs(a // _pymath.gcd(a, b) * b)

        @_nc_callable
        def is_prime_(n):
            n = int(n)
            if n <= 1:
                return False
            if n <= 3:
                return True
            if n % 2 == 0 or n % 3 == 0:
                return False
            i = 5
            w = 2
            while i * i <= n:
                if n % i == 0:
                    return False
                i += w
                w = 6 - w
            return True

        @_nc_callable
        def next_prime_(n):
            n = int(n)
            if n < 2:
                return 2
            k = n + 1
            while True:
                if is_prime_(k):
                    return k
                k += 1

        @_nc_callable
        def primes_upto_(n):
            n = int(n)
            if n < 2:
                return []
            sieve = [True] * (n + 1)
            sieve[0] = sieve[1] = False
            p = 2
            while p * p <= n:
                if sieve[p]:
                    step = p
                    start = p * p
                    for i in range(start, n + 1, step):
                        sieve[i] = False
                p += 1
            return [i for i in range(2, n + 1) if sieve[i]]

        @_nc_callable
        def factorial_(n):
            n = int(n)
            if n < 0:
                return None
            return int(_pymath.factorial(n))

        @_nc_callable
        def ncr_(n, r):
            n = int(n); r = int(r)
            if n < 0 or r < 0 or r > n:
                return None
            r = min(r, n - r)
            num = 1
            den = 1
            for k in range(1, r + 1):
                num *= (n - (r - k))
                den *= k
            return num // den

        @_nc_callable
        def npr_(n, r):
            n = int(n); r = int(r)
            if n < 0 or r < 0 or r > n:
                return None
            out = 1
            for k in range(r):
                out *= (n - k)
            return out

        @_nc_callable
        def quantile_(xs, q):
            arr = [float(x) for x in list(xs)]
            if not arr:
                return None
            arr.sort()
            q = clamp(float(q), 0.0, 1.0)
            pos = (len(arr) - 1) * q
            i = int(_pymath.floor(pos))
            j = int(_pymath.ceil(pos))
            if i == j:
                return arr[i]
            t = pos - i
            return arr[i] + (arr[j] - arr[i]) * t

        @_nc_callable
        def vec_add_(a, b):
            a = list(a); b = list(b)
            return [float(a[0]) + float(b[0]), float(a[1]) + float(b[1])]

        @_nc_callable
        def vec_sub_(a, b):
            a = list(a); b = list(b)
            return [float(a[0]) - float(b[0]), float(a[1]) - float(b[1])]

        @_nc_callable
        def vec_mul_(a, s):
            a = list(a); s = float(s)
            return [float(a[0]) * s, float(a[1]) * s]

        @_nc_callable
        def vec_len2_(v):
            v = list(v)
            return float(v[0]) * float(v[0]) + float(v[1]) * float(v[1])

        @_nc_callable
        def vec_len_(v):
            return float(_pymath.sqrt(vec_len2_(v)))

        @_nc_callable
        def vec_dist_(a, b):
            return vec_len_(vec_sub_(a, b))

        @_nc_callable
        def vec_norm_(v):
            l = vec_len_(v)
            if l == 0.0:
                return None
            return vec_mul_(v, 1.0 / l)

        setattr(m, "clamp", clamp_)
        setattr(m, "clip", clip_)
        setattr(m, "abs", abs_)
        setattr(m, "floor", floor_)
        setattr(m, "ceil", ceil_)
        setattr(m, "sqrt", sqrt_)
        setattr(m, "sin", sin_)
        setattr(m, "cos", cos_)
        setattr(m, "tan", tan_)
        setattr(m, "atan2", atan2_)
        setattr(m, "exp", exp_)
        setattr(m, "log", log_)
        setattr(m, "pow", pow_)
        setattr(m, "lerp", lerp_)
        setattr(m, "mean", mean_)
        setattr(m, "median", median_)
        setattr(m, "var", var_)
        setattr(m, "std", std_)
        setattr(m, "dot", dot_)
        setattr(m, "dist2", dist2_)
        setattr(m, "dist", dist_)
        setattr(m, "sigmoid", sigmoid_)
        setattr(m, "softmax", softmax_)
        setattr(m, "sign", sign_)
        setattr(m, "fract", fract_)
        setattr(m, "mod", mod_)
        setattr(m, "wrap", wrap_)
        setattr(m, "inv_lerp", inv_lerp_)
        setattr(m, "map_range", map_range_)
        setattr(m, "isclose", isclose_)
        setattr(m, "deg2rad", deg2rad_)
        setattr(m, "rad2deg", rad2deg_)
        setattr(m, "sin_deg", sin_deg_)
        setattr(m, "cos_deg", cos_deg_)
        setattr(m, "tan_deg", tan_deg_)
        setattr(m, "normalize_angle_rad", normalize_angle_rad_)
        setattr(m, "normalize_angle_deg", normalize_angle_deg_)
        setattr(m, "softplus", softplus_)
        setattr(m, "smoothstep", smoothstep_)
        setattr(m, "smootherstep", smootherstep_)
        setattr(m, "gauss", gauss_)
        setattr(m, "gcd", gcd_)
        setattr(m, "lcm", lcm_)
        setattr(m, "is_prime", is_prime_)
        setattr(m, "next_prime", next_prime_)
        setattr(m, "primes_upto", primes_upto_)
        setattr(m, "factorial", factorial_)
        setattr(m, "ncr", ncr_)
        setattr(m, "npr", npr_)
        setattr(m, "quantile", quantile_)
        setattr(m, "vec_add", vec_add_)
        setattr(m, "vec_sub", vec_sub_)
        setattr(m, "vec_mul", vec_mul_)
        setattr(m, "vec_len2", vec_len2_)
        setattr(m, "vec_len", vec_len_)
        setattr(m, "vec_dist", vec_dist_)
        setattr(m, "vec_norm", vec_norm_)
        setattr(m, "pi", _pymath.pi)
        setattr(m, "tau", getattr(_pymath, "tau", 2.0*_pymath.pi))
        setattr(m, "e", _pymath.e)
        return m

    def _json_module_object(self):
        class J:
            pass

        j = J()

        @_nc_callable
        def encode_(obj):
            return _json_encode(obj)

        @_nc_callable
        def pretty_(obj):
            return _json_pretty(obj)

        @_nc_callable
        def parse_(s):
            return _json_parse(s)

        @_nc_callable
        def diff_(a, b):
            rows = [["path", "kind", "a", "b"]]
            for r in _json_diff(a, b):
                rows.append(r)
            return rows

        @_nc_callable
        def to_table_(obj, max_rows=400):
            return _json_to_table(obj, int(max_rows))

        @_nc_callable
        def schema_validate_(obj, schema):
            rows = [["path", "error"]]
            for p, e in _schema_validate(obj, schema):
                rows.append([p, e])
            return rows

        @_nc_callable
        def save_(name, obj):
            res = _json_save_secure(name, obj, self.policy, merge=True)
            if not res.get("ok"):
                raise RuntimeError(res.get("error", "json.save failed"))
            return res.get("path")

        @_nc_callable
        def save_replace_(name, obj):
            res = _json_save_secure(name, obj, self.policy, merge=False)
            if not res.get("ok"):
                raise RuntimeError(res.get("error", "json.save_replace failed"))
            return res.get("path")

        @_nc_callable
        def save_info_(name, obj):
            # returns {"ok":bool, "path":str, "tamper_detected":bool, "error":str?}
            return _json_save_secure(name, obj, self.policy, merge=True)

        @_nc_callable
        def load_(name, default=None):
            return _json_load_secure(name, self.policy, default)

        @_nc_callable
        def verify_(name):
            return _json_verify_secure(name, self.policy)

        @_nc_callable
        def last_(name):
            return _json_last_secure(name, self.policy)
        setattr(j, "encode", encode_)
        setattr(j, "pretty", pretty_)
        setattr(j, "parse", parse_)
        setattr(j, "diff", diff_)
        setattr(j, "to_table", to_table_)
        setattr(j, "schema_validate", schema_validate_)
        setattr(j, "save", save_)
        setattr(j, "save_replace", save_replace_)
        setattr(j, "save_info", save_info_)
        setattr(j, "load", load_)
        setattr(j, "verify", verify_)
        setattr(j, "last", last_)
        return j



    def _fs_module_object(self):
        class FS:
            pass
        fs = FS()

        def _root_dir() -> Optional[str]:
            # Prefer policy.data_dir (explicit sandbox dir), else base_dir_current if local.
            dd = self.policy.data_dir
            if dd and not _is_url(dd):
                return os.path.abspath(dd)
            bd = self._base_dir_current
            if bd and (not _is_url(bd)):
                return os.path.abspath(bd)
            return None

        def _safe_path(rel: str) -> str:
            root = _root_dir()
            if root is None:
                raise RuntimeError("fs: no local workspace (URL base). Set policy.data_dir.")
            p = (rel or "").replace("\\", "/").lstrip("/")
            # disallow absolute / drive paths and traversal
            if ":" in p.split("/")[0]:
                raise RuntimeError("fs: blocked absolute/drive path")
            full = os.path.abspath(os.path.join(root, p))
            if not (full == root or full.startswith(root + os.sep)):
                raise RuntimeError("fs: blocked path traversal")
            return full

        @_nc_callable
        def read_(path: str):
            p = _safe_path(str(path))
            with open(p, "r", encoding="utf-8", errors="replace") as f:
                return f.read()

        @_nc_callable
        def write_(path: str, text: Any):
            p = _safe_path(str(path))
            os.makedirs(os.path.dirname(p) or p, exist_ok=True)
            with open(p, "w", encoding="utf-8", errors="replace") as f:
                f.write("" if text is None else str(text))
            return True

        @_nc_callable
        def append_(path: str, text: Any):
            p = _safe_path(str(path))
            os.makedirs(os.path.dirname(p) or p, exist_ok=True)
            with open(p, "a", encoding="utf-8", errors="replace") as f:
                f.write("" if text is None else str(text))
            return True

        @_nc_callable
        def exists_(path: str):
            p = _safe_path(str(path))
            return os.path.exists(p)

        @_nc_callable
        def list_(path: str = ""):
            p = _safe_path(str(path or ""))
            if not os.path.isdir(p):
                return []
            out = []
            for fn in os.listdir(p):
                out.append(fn)
            return out

        @_nc_callable
        def delete_(path: str):
            p = _safe_path(str(path))
            if os.path.isdir(p):
                # only allow deleting empty dirs
                os.rmdir(p)
            else:
                os.remove(p)
            return True

        @_nc_callable
        def replace_(path: str, old: Any, new: Any, count: int = -1):
            p = _safe_path(str(path))
            with open(p, "r", encoding="utf-8", errors="replace") as f:
                s = f.read()
            s2 = s.replace("" if old is None else str(old), "" if new is None else str(new), int(count))
            with open(p, "w", encoding="utf-8", errors="replace") as f:
                f.write(s2)
            return True

        fs.read = read_
        fs.write = write_
        fs.append = append_
        fs.exists = exists_
        fs.list = list_
        fs.delete = delete_
        fs.replace = replace_
        return fs



    def _cam_module_object(self):
        class Cam:
            pass
        cam = Cam()

        @_nc_callable
        def open_(device: str = "default", facing: str = "back", w: int = 1280, h: int = 720, fps: int = 30, title: str = "Camera"):
            # NC itself does not access hardware directly; the GUI host implements camera.*
            try:
                self.ui.window(str(title), 1000, 700, win_id="nc_cam")
            except Exception:
                pass
            self.ui.camera_open(device=str(device), facing=str(facing), w=int(w), h=int(h), fps=int(fps))
            return True

        @_nc_callable
        def close_():
            self.ui.camera_close()
            return True

        @_nc_callable
        def snap_(path: str = "snap.jpg"):
            self.ui.camera_snap(str(path))
            return str(path)

        @_nc_callable
        def record_start_(path: str = "video.mp4", w: int = 1280, h: int = 720, fps: int = 30):
            self.ui.camera_record_start(str(path), int(w), int(h), int(fps))
            return True

        @_nc_callable
        def record_stop_():
            self.ui.camera_record_stop()
            return True

        @_nc_callable
        def set_(key: str, value):
            self.ui.camera_set(str(key), value)
            return True

        setattr(cam, "open", open_)
        setattr(cam, "close", close_)
        setattr(cam, "snap", snap_)
        setattr(cam, "record_start", record_start_)
        setattr(cam, "record_stop", record_stop_)
        setattr(cam, "set", set_)
        return cam


    def _ai_module_object(self):
        # Keeps state in self._ai_state so it persists across runs in this interpreter instance.
        class AI:
            pass

        class Text:
            pass

        ai = AI()
        text_mod = Text()

        # init models if missing
        if "text_model" not in self._ai_state:
            self._ai_state["text_model"] = MarkovTextModel(order=3)
        if "clf" not in self._ai_state:
            self._ai_state["clf"] = SimpleTextClassifier(classes=2, max_vocab=2000)

        @_nc_callable
        def text_init(order):
            self._ai_state["text_model"].reset(order=int(order))
            return True

        @_nc_callable
        def text_reset():
            self._ai_state["text_model"].reset()
            return True

        @_nc_callable
        def text_learn(s):
            self._ai_state["text_model"].learn(str(s))
            return True

        @_nc_callable
        def text_generate(seed, n_tokens):
            return self._ai_state["text_model"].generate(str(seed), int(n_tokens))

        setattr(text_mod, "init", text_init)
        setattr(text_mod, "reset", text_reset)
        setattr(text_mod, "learn", text_learn)
        setattr(text_mod, "generate", text_generate)

        @_nc_callable
        def clf_init(classes, max_vocab=2000):
            self._ai_state["clf"].reset(classes=int(classes), max_vocab=int(max_vocab))
            return True

        @_nc_callable
        def data_add(text, label):
            self._ai_state["clf"].add(str(text), int(label))
            return True

        @_nc_callable
        def train(epochs=20, lr=0.1):
            return self._ai_state["clf"].train(int(epochs), float(lr))

        @_nc_callable
        def predict(text):
            return self._ai_state["clf"].predict(str(text))

        @_nc_callable
        def stats():
            clf: SimpleTextClassifier = self._ai_state["clf"]
            return {"samples": len(clf.data), "vocab": len(clf.vocab), "classes": clf.classes}

        @_nc_callable
        def save(name):
            payload = {
                "text_model": self._ai_state["text_model"].to_json(),
                "clf": self._ai_state["clf"].to_json(),
            }
            path = _safe_json_path(str(name), self.policy)
            raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            if len(raw) > self.policy.max_module_bytes:
                raise RuntimeError("ai.save: blocked (too large)")
            with open(path, "w", encoding="utf-8") as f:
                f.write(json.dumps(payload, ensure_ascii=False, indent=2))
            return path

        @_nc_callable
        def load(name):
            path = _safe_json_path(str(name), self.policy)
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                data = f.read(self.policy.max_module_bytes + 1)
            if len(data.encode("utf-8")) > self.policy.max_module_bytes:
                raise RuntimeError("ai.load: blocked (too large)")
            obj = json.loads(data)
            if isinstance(obj, dict):
                if "text_model" in obj:
                    self._ai_state["text_model"] = MarkovTextModel.from_json(obj["text_model"])
                if "clf" in obj:
                    self._ai_state["clf"] = SimpleTextClassifier.from_json(obj["clf"])
            return True

        setattr(ai, "text", text_mod)
        setattr(ai, "init_classifier", clf_init)
        setattr(ai, "data_add", data_add)
        setattr(ai, "train", train)
        setattr(ai, "predict", predict)
        setattr(ai, "stats", stats)
        setattr(ai, "save", save)
        setattr(ai, "load", load)

        # shorter aliases
        setattr(ai, "add", data_add)
        setattr(ai, "classifier_init", clf_init)

        return ai


    # -------------------------
    # Module loading
    # -------------------------
    def _module_search_paths(self, base: str, extra_paths: Optional[List[str]]) -> List[str]:
        paths: List[str] = []
        if base:
            paths.append(base)
            if not _is_url(base):
                paths.append(os.path.join(base, "libs"))
        if extra_paths:
            paths.extend(extra_paths)
        if os.path.isdir(STANDARD_IMPORTS_DIR):
            paths.append(STANDARD_IMPORTS_DIR)
        out = []
        for p in paths:
            if p and p not in out:
                out.append(p)
        return out

    def _builtin_module(self, name: str) -> Optional[NCModule]:
        if name not in ("ui", "math", "json", "file", "time", "text", "array"):
            return None

        mod = NCModule(name)
        if name == "ui":
            u = self._ui_module_object()
            mod.namespace = {
                key: value
                for key, value in u.__dict__.items()
                if not str(key).startswith("_")
            }
            mod.exports = dict(mod.namespace)
            return mod

        if name == "math":
            m = self._math_module_object()
            exports = {}
            for k, v in m.__dict__.items():
                if not str(k).startswith("_"):
                    exports[k] = v
            mod.namespace = exports
            mod.exports = exports
            return mod

        if name == "json":
            j = self._json_module_object()
            exports = {}
            for k, v in j.__dict__.items():
                if not str(k).startswith("_"):
                    exports[k] = v
            mod.namespace = exports
            mod.exports = exports
            return mod

        if name == "file":
            f = _nc_ext_file_module_object(self)
            exports = {k: v for k, v in f.__dict__.items() if not str(k).startswith("_")}
            mod.namespace = exports
            mod.exports = exports
            return mod

        if name == "time":
            t = _nc_ext_time_module_object(self)
            exports = {k: v for k, v in t.__dict__.items() if not str(k).startswith("_")}
            mod.namespace = exports
            mod.exports = exports
            return mod

        if name == "text":
            t = _nc_ext_text_module_object(self)
            exports = {k: v for k, v in t.__dict__.items() if not str(k).startswith("_")}
            mod.namespace = exports
            mod.exports = exports
            return mod

        if name == "array":
            a = _nc_ext_array_module_object(self)
            exports = {k: v for k, v in a.__dict__.items() if not str(k).startswith("_")}
            mod.namespace = exports
            mod.exports = exports
            return mod
        return None

    def load_module(
        self,
        name: str,
        base: str,
        extra_paths: Optional[List[str]] = None,
        depth: int = 0,
        caller_source: str = "<text>",
        caller_line: int = 1,
    ) -> NCModule:
        if depth > self.policy.max_import_depth:
            raise NCError(_format_source(caller_source), caller_line, "NC blocked: import depth exceeded", self._import_stack)

        builtin = self._builtin_module(name)
        if builtin is not None:
            return builtin

        key = f"mod:{name}@{base}"
        if key in self.module_cache:
            return self.module_cache[key]

        candidates = self._module_search_paths(base, extra_paths)

        mod_text = None
        mod_ref = None

        for b in candidates:
            try:
                ref = _resolve_ref(b, name)
                if _is_url(ref):
                    mod_text = _fetch_url_text(ref, self.policy)
                else:
                    mod_text = _read_file_text(ref, self.policy)
                mod_ref = ref
                break
            except Exception:
                continue

        if mod_text is None:
            if name in BUILTIN_NC_MODULES:
                mod_ref = f"builtin:{name}"
                mod_text = BUILTIN_NC_MODULES[name]
            else:
                raise NCError(_format_source(caller_source), caller_line, f"NC import not found: {name}.nc", self._import_stack)

        module = NCModule(name)
        self.module_cache[key] = module

        parser = NCParser(mod_text, source_name=str(mod_ref))
        stmts = parser.parse()

        if parser.errors:
            err = NCMultiError(parser.errors, header="NC parse errors")
            raise err

        export_names: List[str] = []
        stmts = _expand_repeat_program_top_level(stmts, str(mod_ref))

        env = self.base_env()
        env["__module__"] = module
        env["__exports__"] = export_names
        env["__export_base_keys__"] = set(env.keys())

        if _is_url(mod_ref or ""):
            base_dir = (mod_ref or "").rsplit("/", 1)[0] + "/"
        else:
            base_dir = os.path.dirname(mod_ref) if mod_ref and not _is_url(mod_ref) else base

        with self._with_source(str(mod_ref), push_stack=True):
            self.exec_block(
                stmts,
                env,
                base_dir=base_dir,
                extra_paths=extra_paths,
                in_module=True,
                source_name=str(mod_ref),
            )

        module.namespace = {k: v for k, v in env.items() if not k.startswith("__")}
        module.finalize_exports(export_names)
        return module

    def load_from(
        self,
        src: str,
        name: str,
        base: str,
        extra_paths: Optional[List[str]] = None,
        depth: int = 0,
        caller_source: str = "<text>",
        caller_line: int = 1,
    ) -> NCModule:
        if depth > self.policy.max_import_depth:
            raise NCError(_format_source(caller_source), caller_line, "NC blocked: import depth exceeded", self._import_stack)

        if _is_url(src):
            src_base = src
        else:
            src_base = src if os.path.isabs(src) else os.path.join(base, src)

        if src_base.endswith(".nc"):
            ref = src_base
        else:
            ref = _resolve_ref(src_base, name)

        key = f"from:{ref}"
        if key in self.module_cache:
            return self.module_cache[key]

        try:
            if _is_url(ref):
                text = _fetch_url_text(ref, self.policy)
                new_base = ref.rsplit("/", 1)[0] + "/"
            else:
                text = _read_file_text(ref, self.policy)
                new_base = os.path.dirname(ref)
        except Exception as e:
            self._raise_context_error(e, caller_source, caller_line)

        module = NCModule(name)
        self.module_cache[key] = module

        parser = NCParser(text, source_name=str(ref))
        stmts = parser.parse()
        if parser.errors:
            err = NCMultiError(parser.errors, header="NC parse errors")
            raise err

        export_names: List[str] = []
        env = self.base_env()
        env["__module__"] = module
        env["__exports__"] = export_names
        env["__export_base_keys__"] = set(env.keys())

        with self._with_source(str(ref), push_stack=True):
            self.exec_block(
                stmts,
                env,
                base_dir=new_base,
                extra_paths=extra_paths,
                in_module=True,
                source_name=str(ref),
            )

        module.namespace = {k: v for k, v in env.items() if not k.startswith("__")}
        module.finalize_exports(export_names)
        return module

    # -------------------------
    # Execute with ALWAYS source+line errors
    # -------------------------
    def exec_block(
        self,
        stmts: List[Stmt],
        env: Dict[str, Any],
        base_dir: str = ".",
        extra_paths: Optional[List[str]] = None,
        in_module: bool = False,
        source_name: str = "<text>",
    ):
        self._base_dir_current = base_dir

        # Register every executed block once so forward lookup can scan the
        # whole program of the current source file, not only the current block.
        already_known = False
        for _src, _blk in self._program_blocks:
            if _src == source_name and _blk is stmts:
                already_known = True
                break
        if not already_known:
            self._program_blocks.append((source_name, stmts))

        i = 0
        while i < len(stmts):
            st = stmts[i]
            self._current_block = stmts
            self._current_index = i
            self._tick_steps(1)
            try:
                if st.kind == "inline_row":
                    self._exec_inline_row(st, env, base_dir, extra_paths, in_module, source_name)
                    i += 1
                    continue

                if st.kind in ("button", "checkmark"):
                    group = [st]
                    j = i + 1
                    while j < len(stmts) and stmts[j].kind in ("button", "checkmark"):
                        group.append(stmts[j])
                        j += 1
                    self._exec_button_group(group, env, base_dir, extra_paths, in_module, source_name)
                    i = j
                    continue

                self.exec_stmt(
                    st,
                    env,
                    base_dir=base_dir,
                    extra_paths=extra_paths,
                    in_module=in_module,
                    source_name=source_name,
                )
            except (NCMultiError, NCError, NCEnd):
                raise
            except NCReturn as r:
                if int(getattr(self, "_function_depth", 0)) > 0:
                    raise
                # Keep the historical lenient top-level behavior: a stray
                # return is stored instead of crashing the whole NC program.
                env["__last_return__"] = r.value
            except Exception as e:
                raise NCError(_format_source(source_name), st.line, str(e), self._import_stack) from e
            i += 1

    def _exec_inline_row(self, st: Stmt, env: Dict[str, Any], base_dir: str, extra_paths: Optional[List[str]], in_module: bool, source_name: str):
        """Execute a same-line print/control row in the terminal."""
        items: List[dict] = []
        for raw in list(st.data.get("items") or []):
            kind = str(raw.get("kind") or "button")
            color = env.get("__button_color_all__")
            color_expr = raw.get("color")
            if color_expr:
                try:
                    color = self._eval_expr(str(color_expr), env, self.policy)
                except Exception:
                    color = color_expr

            if kind == "print":
                values: List[str] = []
                for expression in list(raw.get("args") or []):
                    values.append(str(self._eval_expr(str(expression), env, self.policy)))
                items.append({"kind": "print", "values": values, "color": env.get("__text_color__")})
                continue

            if kind == "checkmark":
                name = str(raw.get("name") or "")
                if name and name not in env:
                    env[name] = False
                label = self._eval_expr(str(raw.get("label") or '"Checkmark"'), env, self.policy)
                items.append({
                    "kind": "checkmark",
                    "name": name,
                    "label": "" if label is None else str(label),
                    "checked": bool(env.get(name, False)),
                    "color": color,
                })
                continue

            if kind == "input":
                prompt = self._eval_expr(str(raw.get("prompt") or '"Input"'), env, self.policy)
                items.append({
                    "kind": "input",
                    "name": str(raw.get("name") or ""),
                    "prompt": "" if prompt is None else str(prompt),
                    "color": color,
                })
                continue

            label = self._eval_expr(str(raw.get("label") or '"Button"'), env, self.policy)
            items.append({
                "kind": "button",
                "label": "" if label is None else str(label),
                "color": color,
            })

        selectable = [item for item in items if item.get("kind") in {"button", "checkmark", "input"}]
        if not selectable:
            text = "    ".join(
                " ".join(str(value) for value in item.get("values") or [])
                for item in items
                if item.get("kind") == "print"
            )
            _console_print_colored(text, env.get("__text_color__"))
            return

        def choose_noninteractive() -> dict:
            return selectable[0]

        if not _stdin_is_tty():
            current = choose_noninteractive()
            self._finish_inline_selection(current, selectable, items, env, source_name)
            return

        selected = 0
        while True:
            for item in selectable:
                if item.get("kind") == "checkmark":
                    item["checked"] = bool(env.get(str(item.get("name") or ""), False))
            line = _console_render_inline_row(items, selected)
            print(line)
            key = _console_read_key()
            if key == "left":
                selected = (selected - 1) % len(selectable)
            elif key == "right":
                selected = (selected + 1) % len(selectable)
            elif key in {"enter", "down", "up"} or not key:
                current = selectable[selected]
                if current.get("kind") == "checkmark":
                    name = str(current.get("name") or "")
                    if name:
                        env[name] = not bool(env.get(name, False))
                        current["checked"] = bool(env[name])
                    if _stdout_is_tty():
                        print("\033[1A\033[2K", end="")
                    continue
                self._finish_inline_selection(current, selectable, items, env, source_name)
                return
            if _stdout_is_tty():
                print("\033[1A\033[2K", end="")

    def _finish_inline_selection(self, current: dict, selectable: List[dict], items: List[dict], env: Dict[str, Any], source_name: str):
        kind = str(current.get("kind") or "button")
        if kind == "checkmark":
            name = str(current.get("name") or "")
            if name:
                env[name] = not bool(env.get(name, False))
                current["checked"] = bool(env[name])
            return
        if kind == "input":
            prompt = str(current.get("prompt") or "")
            value = input(prompt + (" " if prompt else ""))
            name = str(current.get("name") or "")
            if name:
                env[name] = value
            env["__last_input__"] = value
            env["__last_input_index__"] = selectable.index(current)
            return
        env["__last_button__"] = str(current.get("label") or "")
        env["__last_button_index__"] = selectable.index(current)

    def _exec_button_group(self, group: List[Stmt], env: Dict[str, Any], base_dir: str, extra_paths: Optional[List[str]], in_module: bool, source_name: str):
        items = []
        for st in group:
            kind = str(st.kind)
            default_label = '"Checkmark"' if kind == "checkmark" else '"Button"'
            try:
                label = self._eval_expr(str(st.data.get("label") or default_label), env, self.policy)
            except Exception:
                label = st.data.get("label") or ("Checkmark" if kind == "checkmark" else "Button")

            color = env.get("__button_color_all__")
            color_expr = st.data.get("color")
            if color_expr:
                try:
                    color = self._eval_expr(str(color_expr), env, self.policy)
                except Exception:
                    color = color_expr

            item = {
                "kind": kind,
                "label": "" if label is None else str(label),
                "color": color,
                "line": st.line,
            }
            if kind == "checkmark":
                var_name = str(st.data.get("name") or "")
                if var_name and var_name not in env:
                    env[var_name] = False
                item["name"] = var_name
                item["checked"] = bool(env.get(var_name, False))
            else:
                item["body"] = list(st.data.get("preparation") or []) + list(st.data.get("action") or [])
            items.append(item)

        if not items:
            return

        if not _stdin_is_tty():
            idx = -1
            for pos, item in enumerate(items):
                if item.get("kind") == "button":
                    idx = pos
                    break
            if idx < 0:
                return
        else:
            selected = 0
            while True:
                for item in items:
                    if item.get("kind") == "checkmark":
                        item["checked"] = bool(env.get(str(item.get("name") or ""), False))
                lines = _console_render_picker_lines(items, selected)
                print()
                for line in lines:
                    print(line)
                key = _console_read_key()
                if key in ("up", "left"):
                    selected = (selected - 1) % len(items)
                elif key in ("down", "right"):
                    selected = (selected + 1) % len(items)
                elif key == "enter" or not key:
                    current = items[selected]
                    if current.get("kind") == "checkmark":
                        var_name = str(current.get("name") or "")
                        if var_name:
                            new_value = not bool(env.get(var_name, False))
                            env[var_name] = new_value
                            current["checked"] = new_value
                        if _stdout_supports_ansi():
                            print(f"[{len(lines)+1}A", end="")
                        continue
                    idx = selected
                    break
                if _stdout_supports_ansi():
                    print(f"[{len(lines)+1}A", end="")

        if idx < 0 or idx >= len(items):
            return
        env["__last_button__"] = items[idx]["label"]
        env["__last_button_index__"] = idx
        self.exec_block(items[idx].get("body") or [], env, base_dir, extra_paths, in_module, source_name)

    def _invoke_zero_arg_action(self, action: Any, env: Optional[Dict[str, Any]] = None):
        if isinstance(action, NCFn):
            return action.call([], runtime_env=env)
        if callable(action):
            return action()
        raise TypeError("repeat action must be callable")

    def exec_stmt(self, st: Stmt, env: Dict[str, Any], base_dir: str, extra_paths: Optional[List[str]], in_module: bool, source_name: str):
        k = st.kind
        d = st.data

        if k == "export":
            if "__exports__" in env and d["name"] not in env["__exports__"]:
                env["__exports__"].append(d["name"])
            return

        if k == "export_all":
            if "__exports__" in env:
                base_keys = set(env.get("__export_base_keys__") or [])
                for name in list(env.keys()):
                    if str(name).startswith("__"):
                        continue
                    if name in base_keys:
                        continue
                    if name not in env["__exports__"]:
                        env["__exports__"].append(name)
            return

        if k == "import":
            name = d["name"]
            mod = self.load_module(
                name,
                base=base_dir,
                extra_paths=extra_paths,
                depth=0,
                caller_source=source_name,
                caller_line=st.line,
            )
            env[name] = mod
            return

        if k == "from_import":
            src = d["base"]
            name = d["name"]
            alias = d.get("as") or name
            mod = self.load_from(
                src,
                name=name,
                base=base_dir,
                extra_paths=extra_paths,
                depth=0,
                caller_source=source_name,
                caller_line=st.line,
            )
            env[alias] = mod
            return

        if k == "let":
            try:
                env[d["name"]] = self._eval_expr(d["expr"], env, self.policy)
            except Exception as e:
                self._raise_context_error(e, source_name, st.line)
            return

        if k == "set":
            try:
                env[d["name"]] = self._eval_expr(d["expr"], env, self.policy)
            except Exception as e:
                self._raise_context_error(e, source_name, st.line)
            return

        if k == "print":
            try:
                parts = []
                for a in d["args"]:
                    v = self._eval_expr(a, env, self.policy)
                    parts.append(str(v))
                _console_print_colored(" ".join(parts), env.get("__text_color__"))
            except Exception as e:
                self._raise_context_error(e, source_name, st.line)
            return

        if k == "return":
            try:
                v = self._eval_expr(d["expr"], env, self.policy)
            except Exception as e:
                self._raise_context_error(e, source_name, st.line)
            raise NCReturn(v)

        if k == "break":
            raise NCBreak()

        if k == "continue":
            raise NCContinue()

        if k == "end":
            raise NCEnd()

        if k == "expr":
            try:
                _ = self._eval_expr(d["expr"], env, self.policy)
            except Exception as e:
                self._raise_context_error(e, source_name, st.line)
            return

        if k == "if":
            try:
                cond_ok = bool(self._eval_expr(d["cond"], env, self.policy))
            except Exception as e:
                self._raise_context_error(e, source_name, st.line)

            if cond_ok:
                self.exec_block(d["then"], env, base_dir, extra_paths, in_module, source_name)
                return
            for c, b, _ln in d["elifs"]:
                try:
                    if bool(self._eval_expr(c, env, self.policy)):
                        self.exec_block(b, env, base_dir, extra_paths, in_module, source_name)
                        return
                except Exception as e:
                    self._raise_context_error(e, source_name, st.line)
            if d["else"] is not None:
                self.exec_block(d["else"], env, base_dir, extra_paths, in_module, source_name)
            return

        if k == "repeat_program":
            return

        if k == "repeat_call":
            try:
                action = self._eval_expr(d["action"], env, self.policy)
                n = int(self._eval_expr(d["n"], env, self.policy))
            except Exception as e:
                self._raise_context_error(e, source_name, st.line)
            for _ in range(max(0, n)):
                try:
                    self._invoke_zero_arg_action(action, env=env)
                except Exception as e:
                    self._raise_context_error(e, source_name, st.line)
            return

        if k == "repeat":
            try:
                n = int(self._eval_expr(d["n"], env, self.policy))
            except Exception as e:
                self._raise_context_error(e, source_name, st.line)
            for _ in range(max(0, n)):
                try:
                    self.exec_block(d["body"], env, base_dir, extra_paths, in_module, source_name)
                except NCContinue:
                    continue
                except NCBreak:
                    break
            return

        if k == "while":
            while True:
                try:
                    if not bool(self._eval_expr(d["cond"], env, self.policy)):
                        break
                except Exception as e:
                    self._raise_context_error(e, source_name, st.line)
                try:
                    self.exec_block(d["body"], env, base_dir, extra_paths, in_module, source_name)
                except NCContinue:
                    continue
                except NCBreak:
                    break
            return

        if k == "for":
            try:
                it = self._eval_expr(d["iter"], env, self.policy)
            except Exception as e:
                self._raise_context_error(e, source_name, st.line)
            # lenient: None => empty loop
            if it is None:
                iterator = iter(())
            # convenience: numbers => range(n)
            elif isinstance(it, (int, float)) and not isinstance(it, bool):
                iterator = iter(range(0, int(it)))
            else:
                try:
                    iterator = iter(it)
                except Exception:
                    raise NCError(_format_source(source_name), st.line, "for expects an iterable (e.g. list/tuple/string). Use range(...) for numbers.", self._import_stack)
            for val in iterator:
                env[d["var"]] = val
                try:
                    self.exec_block(d["body"], env, base_dir, extra_paths, in_module, source_name)
                except NCContinue:
                    continue
                except NCBreak:
                    break
            return

        if k == "fn":
            fn = NCFn(
                name=d["name"],
                arg_names=d["args"],
                body=d["body"],
                closure=dict(env),
                interp=self,
                source=str(source_name),
                line=int(st.line),
                base_dir=str(base_dir or "."),
            )
            env[d["name"]] = fn
            return

        if k == "table":
            tbl = {}
            for key, ex, _ln in d["rows"]:
                try:
                    tbl[key] = self._eval_expr(ex, env, self.policy)
                except Exception as e:
                    self._raise_context_error(e, source_name, _ln)
            env[d["name"]] = tbl
            return

        if k == "text_color_all":
            try:
                env["__text_color__"] = self._eval_expr(d["expr"], env, self.policy)
            except Exception as e:
                self._raise_context_error(e, source_name, st.line)
            return

        if k == "text_color":
            try:
                env["__text_color__"] = self._eval_expr(d["expr"], env, self.policy)
            except Exception as e:
                self._raise_context_error(e, source_name, st.line)
            return

        if k == "button_color_all":
            try:
                env["__button_color_all__"] = self._eval_expr(d["expr"], env, self.policy)
            except Exception as e:
                self._raise_context_error(e, source_name, st.line)
            return

        if k == "button_color":
            # outside a button this is just ignored to stay lenient
            return

        if k == "button":
            return

        if k == "checkmark":
            name = str(d.get("name") or "")
            if name and name not in env:
                env[name] = False
            return

        # UI
        if k == "ui_window":
            win_id = d.get("id") or "nc_sim"
            self.ui.window(d["title"], d["w"], d["h"], win_id=win_id)
            return

        if k == "ui_plot":
            plots = env.setdefault("__ui_plots__", [])
            plots.append((d["series"], d["expr"]))
            return

        if k == "ui_table":
            tables = env.setdefault("__ui_tables__", [])
            tables.append((d["name"], d["vars"]))
            return

        if k == "ui_tick":
            self._ui_tick(env, source_name=source_name, line=st.line)
            return


        # -----------------------------
        # UI Mini DSL (pick/text/anim/ren)
        # -----------------------------
        if k == "textstyle":
            self._ui_doc.add_text_style(d["name"], d.get("decls") or "")
            return

        if k == "anim":
            self._ui_doc.add_anim(d["name"], d.get("body") or "")
            return

        if k == "pick":
            sel_raw = d.get("selector") or ""
            # allow quoted selector or expression
            sel = ""
            try:
                if isinstance(sel_raw, str) and sel_raw.strip().startswith(("'", '"')):
                    sel = str(self._eval_expr(sel_raw, env, self.policy))
                else:
                    sel = str(sel_raw).strip()
            except Exception:
                sel = str(sel_raw).strip()
            node = self._ui_doc.pick(sel)
            self._pick_stack.append(node)
            try:
                self.exec_block(
                    d.get("body") or [],
                    env,
                    base_dir=base_dir,
                    extra_paths=extra_paths,
                    in_module=in_module,
                    source_name=source_name,
                )
            finally:
                if self._pick_stack:
                    self._pick_stack.pop()
            return

        if k in ("pick_text", "pick_html", "pick_css", "pick_use", "pick_anim"):
            if not self._pick_stack:
                # allow using these outside pick (no-op)
                return
            node = self._pick_stack[-1]

            if k == "pick_use":
                nm = str(d.get("name") or "").strip()
                if nm:
                    node["use"] = nm
                return

            if k == "pick_anim":
                nm = str(d.get("name") or "").strip()
                spec_raw = d.get("spec") or ""
                spec = ""
                try:
                    spec = str(self._eval_expr(str(spec_raw), env, self.policy)) if isinstance(spec_raw, str) else str(spec_raw)
                except Exception:
                    spec = str(spec_raw)
                if nm:
                    node["anim"] = {"name": nm, "spec": spec}
                return

            ex = d.get("expr") or ""
            val = None
            try:
                val = self._eval_expr(str(ex), env, self.policy)
            except Exception:
                val = str(ex)

            if k == "pick_text":
                node["text"] = "" if val is None else str(val)
                node["html"] = None
                return
            if k == "pick_html":
                # NCUI2: treat html(...) as plain text (no HTML rendering)
                node["text"] = "" if val is None else str(val)
                return
            if k == "pick_css":
                props = node.get("props") or {}
                props.update(_ui_parse_decls("" if val is None else str(val)))
                node["props"] = props
                return

        if k == "render":
            title = d.get("title")
            if title:
                self._ui_doc.title = str(title)
            w = d.get("w")
            h = d.get("h")
            if w and h:
                try:
                    self._ui_doc.w = int(w)
                    self._ui_doc.h = int(h)
                except Exception:
                    pass
            scene = self._ui_doc.render_scene()
            # Prefer NCUI2 (no HTML/CSS). Host must implement "ui.scene".
            try:
                self.ui.scene_set(scene, title=self._ui_doc.title, w=self._ui_doc.w, h=self._ui_doc.h, win_id="nc_ui")
            except Exception:
                # Fallback: legacy HTML renderer
                html = self._ui_doc.render_html()
                self.ui.html_set(html, title=self._ui_doc.title, w=self._ui_doc.w, h=self._ui_doc.h, win_id="nc_ui")
            return

        # -----------------------------
        # run "<file.nc>"  (execute another file in-place)
        # -----------------------------
        if k == "run_file":
            target = str(d.get("target") or "").strip()
            if not target:
                return
            ref = _resolve_ref(base_dir, target)
            if _is_url(ref):
                code = _fetch_url_text(ref, self.policy)
                new_base = ref.rsplit("/", 1)[0] + "/"
            else:
                code = _read_file_text(ref, self.policy)
                new_base = os.path.dirname(os.path.abspath(ref)) or base_dir

            parser = NCParser(code, source_name=ref)
            stmts2 = parser.parse()
            if parser.errors:
                err = NCMultiError(parser.errors, header="NC parse errors (run)")
                raise err
            with self._with_source(str(ref), push_stack=True):
                self.exec_block(
                    stmts2,
                    env,
                    base_dir=new_base,
                    extra_paths=extra_paths,
                    in_module=in_module,
                    source_name=str(ref),
                )
            return
        # World + Agent
        if k == "world":
            wd = WorldDef(
                name=d["name"],
                state_init=dict(d["state"]),
                actions=list(d["actions"]),
                bounds=dict(d["bounds"]),
                step_body=list(d["step"]),
            )
            env["__worlds__"][wd.name] = wd
            env[wd.name] = wd
            return

        if k == "agent":
            self._run_agent(d["body"], env, base_dir, extra_paths, source_name=source_name)
            return

        raise NCError(_format_source(source_name), st.line, f"Unknown statement kind: {k}", self._import_stack)

    def _ui_tick(self, env: Dict[str, Any], source_name: str, line: int):
        plots = env.get("__ui_plots__", [])
        tables = env.get("__ui_tables__", [])
        step = int(env.get("__ui_step__", 0))

        for series, expr in plots:
            try:
                val = self._eval_expr(expr, env, self.policy)
                self.ui.plot(series, float(val), step)
            except Exception as e:
                self._raise_context_error(e, source_name, line)

        for name, vars_ in tables:
            rows = []
            for v in vars_:
                if v in env:
                    rows.append([v, env[v]])
            self.ui.table(name, rows)

        env["__ui_step__"] = step + 1

    # -------------------------
    # World step execution using NC statements
    # -------------------------
    def _exec_world_step(self, world: WorldDef, state: Dict[str, float], action: str) -> Dict[str, float]:
        env = self.base_env()
        env.update(state)
        env["action"] = action
        bounds = world.bounds

        @_nc_callable
        def clamp_state(var: str, value: float):
            if var not in bounds:
                return float(value)
            lo, hi = bounds[var]
            return clamp(float(value), float(lo), float(hi))

        env["clamp_state"] = clamp_state

        self.exec_block(
            world.step_body,
            env,
            base_dir=".",
            extra_paths=None,
            in_module=False,
            source_name=f"<world:{world.name}.step>",
        )

        new_state = {}
        for k in world.state_init.keys():
            new_state[k] = float(env.get(k, 0.0))
        return new_state

    # -------------------------
    # Agent DSL runner (WorldModel A)
    # -------------------------
    def _run_agent(self, body: List[Stmt], env: Dict[str, Any], base_dir: str, extra_paths: Optional[List[str]], source_name: str):
        cfg = AgentConfig()

        i = 0
        while i < len(body):
            st = body[i]
            if st.kind == "expr":
                s = st.data["expr"].strip()

                m = re.match(r"use\s+world\s+([A-Za-z_]\w*)$", s)
                if m:
                    cfg.world_name = m.group(1)
                    i += 1
                    continue

                m = re.match(r"learn_rate\s+([0-9.]+)$", s)
                if m:
                    cfg.lr = float(m.group(1))
                    i += 1
                    continue

                m = re.match(r"curiosity\s+([0-9.]+)$", s)
                if m:
                    cfg.curiosity = float(m.group(1))
                    i += 1
                    continue

                m = re.match(r"plan\s+horizon\s+(\d+)\s+samples\s+(\d+)$", s)
                if m:
                    cfg.horizon = int(m.group(1))
                    cfg.samples = int(m.group(2))
                    i += 1
                    continue

                m = re.match(r"log\s+(.+)$", s)
                if m:
                    cfg.log_vars = [x.strip() for x in m.group(1).replace(",", " ").split() if x.strip()]
                    i += 1
                    continue

                if s == "tick":
                    cfg.tick = True
                    i += 1
                    continue

                m = re.match(r"loop\s+(\d+)\s*:\s*$", s)
                if m:
                    cfg.steps = int(m.group(1))
                    i += 1
                    continue

            i += 1

        if not cfg.world_name:
            raise NCError(_format_source(source_name), 1, "agent: missing 'use world <Name>'", self._import_stack)

        worlds = env.get("__worlds__", {})
        if cfg.world_name not in worlds:
            raise NCError(_format_source(source_name), 1, f"agent: unknown world '{cfg.world_name}'", self._import_stack)

        world = worlds[cfg.world_name]
        state = dict(world.state_init)
        model = LinearWorldModel(state_keys=list(state.keys()), actions=world.actions, lr=cfg.lr)

        def plan_action() -> str:
            best_a = world.actions[0]
            best_u = -1e18
            for a0 in world.actions:
                total = 0.0
                for _ in range(cfg.samples):
                    s_sim = dict(state)
                    u = 0.0
                    a = a0
                    for _t in range(cfg.horizon):
                        pred = model.predict(s_sim, a)

                        pe = 0.0
                        for kk in model.state_keys:
                            pe += abs(pred[kk] - float(s_sim[kk]))
                        pe /= max(1, len(model.state_keys))
                        u += cfg.curiosity * pe

                        if "energy" in s_sim:
                            u += 2.0 * float(s_sim["energy"])

                        for kk in model.state_keys:
                            s_sim[kk] = pred[kk]
                        a = random.choice(world.actions)
                    total += u
                avg = total / max(1, cfg.samples)
                if avg > best_u:
                    best_u = avg
                    best_a = a0
            return best_a

        for t in range(cfg.steps):
            self._tick_steps(1)

            action = plan_action()
            prev = dict(state)
            state = self._exec_world_step(world, state, action)
            pred_err = model.update(prev, action, state)

            env.update(state)
            env["pred_err"] = float(pred_err)
            env["action"] = action

            if cfg.log_vars:
                parts = [f"t={t:03d}", f"a={action}", f"err={pred_err:.4f}"]
                for v in cfg.log_vars:
                    if v in env:
                        parts.append(f"{v}={env[v]}")
                print(" ".join(parts))
            else:
                print(
                    f"t={t:03d} a={action:>5} err={pred_err:.4f} "
                    + " ".join(f"{k2}={state[k2]:.3f}" for k2 in state.keys())
                )

            if cfg.tick:
                self._ui_tick(env, source_name=source_name, line=1)

            if "energy" in state and state["energy"] <= 0.0:
                print("STOP: energy depleted")
                break


def _expand_repeat_program_top_level(stmts: List[Stmt], source_name: str) -> List[Stmt]:
    repeat_stmts = [st for st in stmts if getattr(st, "kind", "") == "repeat_program"]
    if not repeat_stmts:
        return stmts

    count_stmt = repeat_stmts[-1]
    try:
        n = int(str(count_stmt.data.get("n") or "1").strip())
    except Exception:
        raise NCError(_format_source(source_name), count_stmt.line, "repeat --all needs an integer number", [])

    if n < 0:
        raise NCError(_format_source(source_name), count_stmt.line, "repeat --all cannot be negative", [])

    base_program = [st for st in stmts if getattr(st, "kind", "") != "repeat_program"]
    expanded: List[Stmt] = []
    for _ in range(n):
        expanded.extend(base_program)
    return expanded


# ============================================================
# Public run helpers
# ============================================================

def run_text(
    nc_text: str,
    base: str = ".",
    extra_paths: Optional[List[str]] = None,
    policy: Optional[NCPolicy] = None,
    enable_ui: bool = True,
    source_name: str = "<text>",
) -> Dict[str, Any]:
    preprocess = globals().get("_nc_preprocess_ai_blocks")
    if callable(preprocess):
        nc_text = preprocess(nc_text)
    # Auto-detect MathNC
    if _math_is_triggered(nc_text):
        return run_math_text(
            nc_text,
            base=base,
            extra_paths=extra_paths,
            policy=policy,
            enable_ui=enable_ui,
            source_name=source_name,
        )

    interp = NCInterpreter(policy=policy, enable_ui=enable_ui)
    env = interp.base_env()

    parser = NCParser(nc_text, source_name=source_name)
    stmts = parser.parse()

    if parser.errors:
        err = NCMultiError(parser.errors, header="NC parse errors")
        raise err

    stmts = _expand_repeat_program_top_level(stmts, str(source_name))

    with interp._with_source(str(source_name), push_stack=False):
        try:
            interp.exec_block(
                stmts,
                env,
                base_dir=base,
                extra_paths=extra_paths,
                in_module=False,
                source_name=str(source_name),
            )
        except NCEnd:
            env["__ended__"] = True
    return env


def run_file(
    path: str,
    policy: Optional[NCPolicy] = None,
    enable_ui: bool = True,
    extra_paths: Optional[List[str]] = None,
) -> Dict[str, Any]:
    policy = policy or NCPolicy()
    abs_path = os.path.abspath(path)
    base = os.path.dirname(abs_path) or os.getcwd()
    text = _read_file_text(abs_path, policy)
    return run_text(
        text,
        base=base,
        extra_paths=extra_paths,
        policy=policy,
        enable_ui=enable_ui,
        source_name=abs_path,
    )


def run_url(
    url: str,
    policy: Optional[NCPolicy] = None,
    enable_ui: bool = True,
    extra_paths: Optional[List[str]] = None,
) -> Dict[str, Any]:
    policy = policy or NCPolicy()
    base = url.rsplit("/", 1)[0] + "/"
    text = _fetch_url_text(url, policy)
    return run_text(
        text,
        base=base,
        extra_paths=extra_paths,
        policy=policy,
        enable_ui=enable_ui,
        source_name=url,
    )


# ============================================================
# Learn mode (built-in)
# ============================================================

_LEARN = {
    "basics": [
        ("Variables", 'let x = 1\nlet y = 2\nprint x, y'),
        ("If/Repeat", 'let count = 0\nrepeat 5:\n  set count = count + 1\nwhen count == 5:\n  print "ok"\nelse:\n  print "no"'),
        ("Repeat Action", 'fn ping():\n  print "hi"\n\nrepeat (ping) 3 times\nrepeat = wiederhole\ntimes = mal\nwiederhole (ping) 2 mal'),
    ],
    "imports": [
        ("Imports", 'import ui\n# ui is a module; exports accessible via ui.window(...)\nui.window("Hello", 600, 400)'),
    ],
    "ai_quickstart": [
        ("AI Quickstart", 'import ai\nai.init_classifier()\nai.add("greeting", "hello there")\nai.add("farewell", "goodbye friend")\nai.train()\nprint ai.predict("hello")'),
    ],
    "json": [
        (
            "JSON Diff + Table",
            r'''
import ui
import json

let a = {"x":1, "y":2, "inv":["potion"]}
let b = {"x":1, "y":3, "inv":["potion","gold"]}

ui.window("JSON Demo", 1000, 650)
ui.table("diff", json.diff(a,b))
ui.table("b", json.to_table(b, 200))
'''.strip(),
        ),
    ],
    "worldmodel": [
        (
            "World + Agent",
            r'''
world MiniGrid:
  state x=0, y=0, energy=1.0
  actions [up,down,left,right,rest]
  bounds x 0..4
  bounds y 0..4
  step:
    if action == "rest":
      set energy = energy - 0.002
    else:
      set energy = energy - 0.01

    if action == "up":
      set y = clamp_state("y", y-1)
    if action == "down":
      set y = clamp_state("y", y+1)
    if action == "left":
      set x = clamp_state("x", x-1)
    if action == "right":
      set x = clamp_state("x", x+1)

window "World Sim" size 1000 650
plot "energy" energy
plot "pred_err" pred_err
table "state" [x,y,energy,pred_err]
tick

agent:
  use world MiniGrid
  learn_rate 0.05
  curiosity 1.0
  plan horizon 6 samples 30
  log x y energy pred_err
  tick
'''.strip(),
        ),
    ],
}


def run_learn(topic: Optional[str] = None):
    if not topic:
        print("NC Learn Mode topics:", ", ".join(sorted(_LEARN.keys())))
        print('Example: run_learn("worldmodel")')
        return
    topic = topic.strip().lower()
    if topic not in _LEARN:
        print("Unknown topic:", topic)
        print("Available:", ", ".join(sorted(_LEARN.keys())))
        return
    for title, code in _LEARN[topic]:
        print("\n==", title, "==\n")
        print(code)


# ------------------------------------------------------------
# Backward compatibility for older nc_console.py (v0.1 style)
# ------------------------------------------------------------
def run_nc_text(
    nc_text: str,
    base: str = ".",
    search_paths: Optional[List[str]] = None,
    policy: Optional[NCPolicy] = None,
    enable_ui: bool = True,
) -> Dict[str, Any]:
    return run_text(
        nc_text,
        base=base,
        extra_paths=search_paths,
        policy=policy,
        enable_ui=enable_ui,
        source_name="<text>",
    )


# ============================================================
# NC AI Main Patch (2026-04-05)
# Adds AI-first preprocessing, stronger stdlib, debug helpers,
# package/python bridge, vector memory, simple RAG/chat flow.
# ============================================================
import traceback as _nc_traceback
import importlib as _nc_importlib
import urllib.parse as _nc_urlparse
from pathlib import Path as _NCPath

class _NCVectorStore:
    def __init__(self):
        self.items = []

    def add(self, text, meta=None):
        vec = _nc_embed_text(text)
        row = {"text": "" if text is None else str(text), "meta": meta if isinstance(meta, dict) else {}, "vec": vec}
        self.items.append(row)
        return row

    def search(self, query, top_k=5):
        qv = _nc_embed_text(query)
        scored = []
        for row in self.items:
            scored.append({
                "text": row["text"],
                "meta": dict(row.get("meta") or {}),
                "score": _nc_cosine(qv, row.get("vec") or []),
            })
        scored.sort(key=lambda x: x.get("score", 0.0), reverse=True)
        return scored[:max(1, int(top_k))]


def _nc_hash_tokens(tokens, dims=64):
    vec = [0.0] * max(8, int(dims))
    for tok in tokens:
        h = int(hashlib.sha256(tok.encode('utf-8')).hexdigest(), 16)
        idx = h % len(vec)
        sign = -1.0 if ((h >> 8) & 1) else 1.0
        vec[idx] += sign * (1.0 + ((h >> 16) % 5) * 0.1)
    return vec


def _nc_embed_text(text, dims=64):
    toks = _tokenize("" if text is None else str(text))
    if not toks:
        return [0.0] * max(8, int(dims))
    vec = _nc_hash_tokens(toks, dims=max(8, int(dims)))
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


def _nc_cosine(a, b):
    if not a or not b:
        return 0.0
    n = min(len(a), len(b))
    return float(sum(float(a[i]) * float(b[i]) for i in range(n)))


def _nc_default_ai_state():
    return {
        "model": None,
        "datasets": [],
        "memory_name": None,
        "memory_entries": [],
        "tools": [],
        "chat": [],
        "last_generated": "",
        "last_embedding": [],
        "last_rag": [],
        "vector_store": _NCVectorStore(),
        "profile": {"runs": 0, "timings": []},
        "dataset_rows": [],
    }


def _nc_ai_state_for(interp):
    if not isinstance(getattr(interp, '_ai_state', None), dict):
        interp._ai_state = {}
    if not interp._ai_state:
        interp._ai_state.update(_nc_default_ai_state())
    if not isinstance(interp._ai_state.get('vector_store'), _NCVectorStore):
        interp._ai_state['vector_store'] = _NCVectorStore()
    if not isinstance(interp._ai_state.get('datasets'), list):
        interp._ai_state['datasets'] = []
    if not isinstance(interp._ai_state.get('memory_entries'), list):
        interp._ai_state['memory_entries'] = []
    if not isinstance(interp._ai_state.get('chat'), list):
        interp._ai_state['chat'] = []
    if not isinstance(interp._ai_state.get('tools'), list):
        interp._ai_state['tools'] = []
    if not isinstance(interp._ai_state.get('profile'), dict):
        interp._ai_state['profile'] = {"runs": 0, "timings": []}
    if not isinstance(interp._ai_state.get('dataset_rows'), list):
        interp._ai_state['dataset_rows'] = []
    return interp._ai_state


def _nc_friendly_exc(exc, source='<text>', line=1):
    head = f"NC runtime error at {source}:{line}: {exc}"
    tb = _nc_traceback.format_exc().strip()
    return head + ("\nNC stacktrace:\n" + tb if tb else "")


def _nc_try_parse_json_text(s, default=None):
    try:
        return json.loads(s)
    except Exception:
        return default


def _nc_read_maybe_json(path):
    p = _NCPath(path)
    raw = p.read_text(encoding='utf-8', errors='replace')
    return raw, _nc_try_parse_json_text(raw, None)


def _nc_preprocess_ai_blocks(nc_text: str) -> str:
    lines = (nc_text or '').splitlines()
    out = []
    i = 0
    while i < len(lines):
        raw = lines[i]
        indent = len(raw) - len(raw.lstrip(' '))
        s = raw.strip()
        if not s or s.startswith('#'):
            out.append(raw)
            i += 1
            continue

        def collect_block(start_i):
            blk = []
            j = start_i + 1
            while j < len(lines):
                rr = lines[j]
                ss = rr.strip()
                ind = len(rr) - len(rr.lstrip(' '))
                if ss and ind <= indent:
                    break
                blk.append(rr)
                j += 1
            return blk, j

        m = re.match(r'model\s+(.+)$', s)
        if m and not s.endswith(':'):
            out.append(' ' * indent + f'let __nc_ai_model = ai_runtime_set_model({m.group(1)})')
            i += 1
            continue
        m = re.match(r'dataset\s+(.+)$', s)
        if m and not s.endswith(':'):
            out.append(' ' * indent + f'let __nc_ai_dataset = ai_runtime_add_dataset({m.group(1)})')
            i += 1
            continue
        m = re.match(r'memory\s+(.+)$', s)
        if m and not s.endswith(':'):
            out.append(' ' * indent + f'let __nc_ai_memory = ai_runtime_set_memory({m.group(1)})')
            i += 1
            continue
        m = re.match(r'tool\s+(.+)$', s)
        if m and not s.endswith(':'):
            out.append(' ' * indent + f'let __nc_ai_tool = ai_runtime_add_tool({m.group(1)})')
            i += 1
            continue
        m = re.match(r'embed\s+(.+?)(?:\s+as\s+([A-Za-z_]\w*))?$', s)
        if m and not s.endswith(':'):
            target = m.group(2) or '__nc_last_embedding'
            out.append(' ' * indent + f'let {target} = ai_runtime_embed({m.group(1)})')
            i += 1
            continue
        m = re.match(r'generate\s+(.+?)(?:\s+as\s+([A-Za-z_]\w*))?$', s)
        if m and not s.endswith(':'):
            target = m.group(2) or '__nc_last_generation'
            out.append(' ' * indent + f'let {target} = ai_runtime_generate({m.group(1)})')
            i += 1
            continue
        m = re.match(r'rag\s+(.+?)(?:\s+top\s+(\d+))?(?:\s+as\s+([A-Za-z_]\w*))?$', s)
        if m and not s.endswith(':'):
            qexpr = m.group(1)
            top_k = m.group(2) or '5'
            target = m.group(3) or '__nc_last_rag'
            out.append(' ' * indent + f'let {target} = ai_runtime_rag({qexpr}, {top_k})')
            i += 1
            continue
        if s == 'train':
            out.append(' ' * indent + 'let __nc_ai_train = ai_runtime_train()')
            i += 1
            continue
        if s.startswith('chat:'):
            block, j = collect_block(i)
            out.append(' ' * indent + 'let __nc_chat_begin = ai_runtime_chat_begin()')
            for rr in block:
                ss = rr.strip()
                rel_indent = ' ' * indent
                m2 = re.match(r'(system|user|assistant)\s+(.+)$', ss)
                if m2:
                    role = m2.group(1)
                    expr = m2.group(2)
                    out.append(rel_indent + f'let __nc_chat_{role} = ai_runtime_chat_message("{role}", {expr})')
                else:
                    out.append(rr)
            i = j
            continue
        i += 1
        out.append(raw)
    return '\n'.join(out)


_ORIG_BASE_ENV = NCInterpreter.base_env
_ORIG_EXEC_BLOCK = NCInterpreter.exec_block


def _nc_ai_save_state(interp):
    st = _nc_ai_state_for(interp)
    data_dir = interp.policy.data_dir or os.path.join(os.getcwd(), 'data')
    os.makedirs(data_dir, exist_ok=True)
    mem_name = st.get('memory_name') or 'default_memory'
    path = os.path.join(data_dir, f'nc_ai_memory_{mem_name}.json')
    payload = {
        'model': st.get('model'),
        'datasets': list(st.get('datasets') or []),
        'memory_name': st.get('memory_name'),
        'memory_entries': list(st.get('memory_entries') or []),
        'chat': list(st.get('chat') or []),
        'tools': list(st.get('tools') or []),
        'dataset_rows': list(st.get('dataset_rows') or []),
    }
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    return path


def _nc_ai_load_state(interp, name=None):
    st = _nc_ai_state_for(interp)
    data_dir = interp.policy.data_dir or os.path.join(os.getcwd(), 'data')
    os.makedirs(data_dir, exist_ok=True)
    mem_name = name or st.get('memory_name') or 'default_memory'
    path = os.path.join(data_dir, f'nc_ai_memory_{mem_name}.json')
    if not os.path.isfile(path):
        return None
    raw = json.load(open(path, 'r', encoding='utf-8'))
    if isinstance(raw, dict):
        st['model'] = raw.get('model')
        st['datasets'] = list(raw.get('datasets') or [])
        st['memory_name'] = raw.get('memory_name') or mem_name
        st['memory_entries'] = list(raw.get('memory_entries') or [])
        st['chat'] = list(raw.get('chat') or [])
        st['tools'] = list(raw.get('tools') or [])
        st['dataset_rows'] = list(raw.get('dataset_rows') or [])
        st['vector_store'] = _NCVectorStore()
        for row in st['memory_entries']:
            txt = row.get('text') if isinstance(row, dict) else row
            st['vector_store'].add(txt, row if isinstance(row, dict) else {})
    return path


def _nc_new_base_env(self):
    env = _ORIG_BASE_ENV(self)
    st = _nc_ai_state_for(self)

    @_nc_callable
    def ai_runtime_set_model(name):
        st['model'] = '' if name is None else str(name)
        return st['model']

    @_nc_callable
    def ai_runtime_add_dataset(spec):
        value = spec
        if isinstance(spec, str):
            s = spec.strip()
            p = s
            if not os.path.isabs(p):
                p = os.path.join(self._base_dir_current or '.', p)
            if os.path.isfile(p):
                raw, parsed = _nc_read_maybe_json(p)
                value = parsed if parsed is not None else raw
        st['datasets'].append(value)
        if isinstance(value, list):
            for row in value:
                st['dataset_rows'].append(row)
                txt = row.get('text') if isinstance(row, dict) else str(row)
                st['vector_store'].add(txt, row if isinstance(row, dict) else {'value': row})
        elif isinstance(value, dict):
            rows = value.get('rows') if isinstance(value.get('rows'), list) else [value]
            for row in rows:
                st['dataset_rows'].append(row)
                txt = row.get('text') if isinstance(row, dict) else str(row)
                st['vector_store'].add(txt, row if isinstance(row, dict) else {'value': row})
        else:
            st['vector_store'].add(str(value), {'source': 'dataset'})
        return value

    @_nc_callable
    def ai_runtime_set_memory(name):
        st['memory_name'] = '' if name is None else str(name)
        _nc_ai_load_state(self, st['memory_name'])
        return st['memory_name']

    @_nc_callable
    def ai_runtime_add_tool(name):
        tool_name = '' if name is None else str(name)
        if tool_name not in st['tools']:
            st['tools'].append(tool_name)
        return tool_name

    @_nc_callable
    def ai_runtime_chat_begin():
        st['chat'] = []
        return True

    @_nc_callable
    def ai_runtime_chat_message(role, content):
        msg = {'role': str(role), 'content': '' if content is None else str(content)}
        st['chat'].append(msg)
        return msg

    @_nc_callable
    def ai_runtime_embed(value):
        vec = _nc_embed_text(value)
        st['last_embedding'] = vec
        return vec

    @_nc_callable
    def ai_runtime_train():
        started = time.time()
        rows = st.get('dataset_rows') or []
        texts = []
        labels = []
        for row in rows:
            if isinstance(row, dict):
                txt = row.get('text') or row.get('input') or row.get('prompt') or ''
                lab = row.get('label') or row.get('class') or row.get('tag') or 'default'
            else:
                txt = str(row)
                lab = 'default'
            texts.append(str(txt))
            labels.append(str(lab))
            st['vector_store'].add(txt, {'label': lab, 'source': 'train'})
        unique_labels = sorted(set(labels))
        label_to_idx = {lab:i for i, lab in enumerate(unique_labels)}
        clf = SimpleTextClassifier(classes=max(2, len(unique_labels)))
        for txt, lab in zip(texts, labels):
            clf.add(txt, label_to_idx.get(lab, 0))
        if texts:
            clf.train(epochs=max(8, min(40, len(texts) * 2)), lr=0.2)
        st['classifier'] = {'model': clf, 'labels': unique_labels}
        duration = time.time() - started
        st['profile']['runs'] = int(st['profile'].get('runs', 0)) + 1
        st['profile'].setdefault('timings', []).append({'op': 'train', 'seconds': duration, 'rows': len(texts)})
        return {'trained_rows': len(texts), 'seconds': duration, 'labels': sorted(set(labels))}

    @_nc_callable
    def ai_runtime_generate(prompt):
        prompt = '' if prompt is None else str(prompt)
        result = ''
        clf_pack = st.get('classifier')
        if isinstance(clf_pack, dict) and clf_pack.get('model') is not None:
            clf = clf_pack.get('model')
            labels = list(clf_pack.get('labels') or [])
            pred = clf.predict(prompt)
            if isinstance(pred, list) and labels:
                best_idx = max(range(len(pred)), key=lambda i: pred[i])
                best = labels[best_idx] if best_idx < len(labels) else str(best_idx)
            else:
                best = labels[1] if labels and float(pred or 0) >= 0.5 and len(labels) > 1 else (labels[0] if labels else 'unknown')
            result = f"[{st.get('model') or 'nc-ai'}] class={best}; prompt={prompt}"
        else:
            recent = ' | '.join(msg['content'] for msg in st.get('chat', [])[-3:])
            result = f"[{st.get('model') or 'nc-ai'}] {prompt}" + (f" :: context={recent}" if recent else '')
        st['last_generated'] = result
        if st.get('memory_name'):
            st['memory_entries'].append({'text': prompt, 'kind': 'prompt'})
            st['memory_entries'].append({'text': result, 'kind': 'response'})
            st['vector_store'].add(prompt, {'kind': 'prompt'})
            st['vector_store'].add(result, {'kind': 'response'})
            _nc_ai_save_state(self)
        return result

    @_nc_callable
    def ai_runtime_rag(query, top_k=5):
        res = st['vector_store'].search(query, top_k)
        st['last_rag'] = res
        return res

    class _HttpMod: pass
    http = _HttpMod()
    @_nc_callable
    def http_get(url, headers=None):
        req = urllib.request.Request(str(url), headers=headers if isinstance(headers, dict) else {'User-Agent': 'NC/AI'})
        with urllib.request.urlopen(req, timeout=self.policy.url_timeout_sec) as r:
            return r.read().decode('utf-8', errors='replace')
    @_nc_callable
    def http_json(url, headers=None):
        return _nc_try_parse_json_text(http_get(url, headers), {})
    http.get = http_get; http.json = http_json

    class _VectorMod: pass
    vector = _VectorMod()
    @_nc_callable
    def vector_add(text, meta=None):
        return st['vector_store'].add(text, meta if isinstance(meta, dict) else {})
    @_nc_callable
    def vector_search(query, top_k=5):
        return st['vector_store'].search(query, top_k)
    @_nc_callable
    def vector_embed(text):
        return _nc_embed_text(text)
    vector.add = vector_add; vector.search = vector_search; vector.embed = vector_embed

    class _TokenMod: pass
    token = _TokenMod()
    @_nc_callable
    def tokenize_text(value):
        return _tokenize('' if value is None else str(value))
    @_nc_callable
    def count_tokens(value):
        return len(tokenize_text(value))
    token.split = tokenize_text; token.count = count_tokens

    class _DataMod: pass
    data = _DataMod()
    @_nc_callable
    def load_data(path):
        p = str(path)
        if not os.path.isabs(p):
            p = os.path.join(self._base_dir_current or '.', p)
        raw, parsed = _nc_read_maybe_json(p)
        return parsed if parsed is not None else raw
    @_nc_callable
    def save_data(path, value):
        p = str(path)
        if not os.path.isabs(p):
            p = os.path.join(self._base_dir_current or '.', p)
        os.makedirs(os.path.dirname(p) or '.', exist_ok=True)
        with open(p, 'w', encoding='utf-8') as f:
            if isinstance(value, (dict, list)):
                json.dump(value, f, ensure_ascii=False, indent=2)
            else:
                f.write('' if value is None else str(value))
        return p
    data.load = load_data; data.save = save_data

    class _DBMod: pass
    dbmod = _DBMod()
    @_nc_callable
    def db_open(path):
        import sqlite3
        p = str(path)
        if not os.path.isabs(p):
            p = os.path.join(self._base_dir_current or '.', p)
        return sqlite3.connect(p)
    @_nc_callable
    def db_query(conn, sql):
        cur = conn.cursor(); cur.execute(str(sql))
        try:
            rows = cur.fetchall()
        except Exception:
            conn.commit(); rows = []
        return rows
    dbmod.open = db_open; dbmod.query = db_query

    class _ImageMod: pass
    image = _ImageMod()
    @_nc_callable
    def image_info(path):
        p = str(path)
        if not os.path.isabs(p):
            p = os.path.join(self._base_dir_current or '.', p)
        try:
            from PIL import Image
            im = Image.open(p)
            return {'width': im.size[0], 'height': im.size[1], 'mode': im.mode, 'format': im.format}
        except Exception as e:
            return {'error': str(e)}
    image.info = image_info

    class _AudioMod: pass
    audio = _AudioMod()
    @_nc_callable
    def audio_info(path):
        p = str(path)
        if not os.path.isabs(p):
            p = os.path.join(self._base_dir_current or '.', p)
        return {'path': p, 'exists': os.path.isfile(p), 'bytes': os.path.getsize(p) if os.path.isfile(p) else 0}
    audio.info = audio_info

    class _PyMod: pass
    py = _PyMod()
    @_nc_callable
    def py_import(name):
        return _nc_importlib.import_module(str(name))
    @_nc_callable
    def py_call(mod, name, args=None):
        fn = getattr(mod, str(name))
        arr = list(args) if isinstance(args, (list, tuple)) else ([] if args is None else [args])
        return fn(*arr)
    py.import_module = py_import; py.call = py_call

    class _PkgMod: pass
    pkg = _PkgMod()
    @_nc_callable
    def pkg_install(name):
        return {'status': 'manual', 'message': 'Use pip in Python env for now', 'package': str(name)}
    @_nc_callable
    def pkg_import(name):
        return py_import(name)
    pkg.install = pkg_install; pkg.import_module = pkg_import

    class _DebugMod: pass
    debug = _DebugMod()
    @_nc_callable
    def vars_snapshot(env_map=None):
        src = env if not isinstance(env_map, dict) else env_map
        out = {}
        for k, v in src.items():
            if str(k).startswith('__'):
                continue
            try:
                out[k] = repr(v)
            except Exception:
                out[k] = '<unrepr>'
        return out
    @_nc_callable
    def stacktrace(message=''):
        return {'message': str(message), 'source': self._current_source, 'steps': self.step_counter, 'import_stack': list(self._import_stack)}
    @_nc_callable
    def profile_summary():
        return dict(st.get('profile') or {})
    debug.vars = vars_snapshot; debug.stacktrace = stacktrace; debug.profile = profile_summary

    class _TestMod: pass
    test = _TestMod()
    @_nc_callable
    def assert_equal(a, b, message=''):
        if a != b:
            raise AssertionError(message or f'Expected {a!r} == {b!r}')
        return True
    @_nc_callable
    def assert_true(v, message=''):
        if not _nc_to_bool(v):
            raise AssertionError(message or 'Expected truthy value')
        return True
    test.eq = assert_equal; test.true = assert_true

    class _ModelMod: pass
    modelmod = _ModelMod()
    @_nc_callable
    def current_model():
        return st.get('model')
    @_nc_callable
    def generate_text(prompt):
        return ai_runtime_generate(prompt)
    modelmod.current = current_model; modelmod.generate = generate_text

    env.update({
        'ai_runtime_set_model': ai_runtime_set_model,
        'ai_runtime_add_dataset': ai_runtime_add_dataset,
        'ai_runtime_set_memory': ai_runtime_set_memory,
        'ai_runtime_add_tool': ai_runtime_add_tool,
        'ai_runtime_chat_begin': ai_runtime_chat_begin,
        'ai_runtime_chat_message': ai_runtime_chat_message,
        'ai_runtime_embed': ai_runtime_embed,
        'ai_runtime_train': ai_runtime_train,
        'ai_runtime_generate': ai_runtime_generate,
        'ai_runtime_rag': ai_runtime_rag,
        'http': http,
        'vector': vector,
        'token': token,
        'data': data,
        'db': dbmod,
        'image': image,
        'audio': audio,
        'py': py,
        'pkg': pkg,
        'debug': debug,
        'test': test,
        'model_api': modelmod,
        'package': pkg,
    })
    return env


def _nc_exec_block_with_debug(self, stmts, env, base_dir='.', extra_paths=None, in_module=False, source_name='<text>'):
    started = time.time()
    try:
        return _ORIG_EXEC_BLOCK(self, stmts, env, base_dir=base_dir, extra_paths=extra_paths, in_module=in_module, source_name=source_name)
    except Exception as e:
        # Keep internal control-flow exceptions untouched so the original
        # runtime can handle them at the correct layer.
        if isinstance(e, (NCError, NCMultiError, NCBreak, NCContinue, NCReturn, NCEnd)):
            raise
        line = 1
        try:
            line = int(env.get('__line__', 1))
        except Exception:
            pass
        raise NCError(_format_source(source_name), line, _nc_friendly_exc(e, source=source_name, line=line), import_stack=getattr(self, '_import_stack', None))
    finally:
        st = _nc_ai_state_for(self)
        st['profile']['runs'] = int(st['profile'].get('runs', 0)) + 1
        st['profile'].setdefault('timings', []).append({'op': 'exec_block', 'seconds': time.time() - started, 'source': str(source_name)})


NCInterpreter.base_env = _nc_new_base_env
NCInterpreter.exec_block = _nc_exec_block_with_debug

# extend learn mode
try:
    _LEARN.setdefault('ai_first', []).append((
        'AI First Syntax',
        '\n'.join([
            'model "local-nc-ai"',
            'memory "project_memory"',
            'dataset "train.json"',
            'tool "web"',
            'tool "files"',
            'chat:',
            '  system "You are a coding helper."',
            '  user "Write a tiny Python app."',
            'train',
            'generate "Create an answer" as answer',
            'print answer',
            'rag "coding helper" top 3 as hits',
            'print hits',
        ])
    ))
except Exception:
    pass


# ---- missing stdlib module fallbacks for this nc.py build ----
def _nc_make_simple_module(**kwargs):
    class _M: pass
    m = _M()
    for k, v in kwargs.items():
        setattr(m, k, v)
    return m

def _nc_file_module_object(self):
    @_nc_callable
    def read(path):
        p = str(path)
        if not os.path.isabs(p):
            p = os.path.join(self._base_dir_current or '.', p)
        with open(p, 'r', encoding='utf-8', errors='replace') as f:
            return f.read()
    @_nc_callable
    def write(path, value):
        p = str(path)
        if not os.path.isabs(p):
            p = os.path.join(self._base_dir_current or '.', p)
        os.makedirs(os.path.dirname(p) or '.', exist_ok=True)
        with open(p, 'w', encoding='utf-8') as f:
            f.write('' if value is None else str(value))
        return p
    @_nc_callable
    def exists(path):
        p = str(path)
        if not os.path.isabs(p):
            p = os.path.join(self._base_dir_current or '.', p)
        return os.path.exists(p)
    return _nc_make_simple_module(read=read, write=write, exists=exists)

def _nc_time_module_object(self):
    @_nc_callable
    def now_ms():
        return int(time.time() * 1000)
    @_nc_callable
    def sleep_ms(ms):
        time.sleep(max(0.0, float(ms) / 1000.0)); return True
    return _nc_make_simple_module(now_ms=now_ms, sleep_ms=sleep_ms)

def _nc_text_module_object(self):
    return _nc_make_simple_module(lower=_nc_callable(lambda s='': str(s).lower()), upper=_nc_callable(lambda s='': str(s).upper()), split=_nc_callable(lambda s='', sep=' ': str(s).split(str(sep))), join=_nc_callable(lambda arr, sep=' ': str(sep).join(str(x) for x in (arr or []))))

def _nc_array_module_object(self):
    return _nc_make_simple_module(push=push, pop=pop, len=_nc_callable(lambda arr=None: len(arr or [])))

def _nc_net_module_object(self):
    @_nc_callable
    def get(url):
        req = urllib.request.Request(str(url), headers={'User-Agent':'NC/AI'})
        with urllib.request.urlopen(req, timeout=self.policy.url_timeout_sec) as r:
            return r.read().decode('utf-8', errors='replace')
    return _nc_make_simple_module(get=get)

def _nc_game_module_object(self):
    return _nc_make_simple_module(score=_nc_callable(lambda current=0, add=1: (current or 0) + (add or 0)))

def _nc_sound_module_object(self):
    return _nc_make_simple_module(play=_nc_callable(lambda path='': {'playing': str(path)}))


# -------------------------------------------------
# SAFE MODULE INJECTION (does NOT overwrite existing implementations)
# -------------------------------------------------

def _nc_safe_setattr(cls, name, value):
    try:
        existing = getattr(cls, name, None)

        # do not overwrite working implementations
        if callable(existing):
            return

        # allow overwrite only if missing or None
        if existing is None:
            setattr(cls, name, value)

    except Exception:
        # last fallback: do nothing
        pass


_nc_safe_setattr(NCInterpreter, "_file_module_object", _nc_file_module_object)
_nc_safe_setattr(NCInterpreter, "_time_module_object", _nc_time_module_object)
_nc_safe_setattr(NCInterpreter, "_text_module_object", _nc_text_module_object)
_nc_safe_setattr(NCInterpreter, "_array_module_object", _nc_array_module_object)
_nc_safe_setattr(NCInterpreter, "_net_module_object", _nc_net_module_object)
_nc_safe_setattr(NCInterpreter, "_game_module_object", _nc_game_module_object)
_nc_safe_setattr(NCInterpreter, "_sound_module_object", _nc_sound_module_object)



# --- compatibility fixes for this build ---
math = _pymath
if '_friendly_error_message' not in globals():
    def _friendly_error_message(msg: str) -> str:
        s = '' if msg is None else str(msg)
        hints = globals().get('_FRIENDLY_ERROR_HINTS') or []
        for needle, hint in hints:
            if needle in s:
                return s + ' | Hint: ' + hint
        return s



# ============================================================
# NC stdlib extension patch (2026-04-10)
# Adds:
# - global helpers: str/to_str, float/to_float, bool/to_bool, round, abs,
#   min, max, contains, startswith, endswith, replace, split, join, keys,
#   values, typeof
# - modules: text, array, file, time
# - checkmark persistence without json: check_set/check_get/check_toggle
# - bool aliases: true/false
# - bugfix: import text/array/file/time as real builtin modules
# - bugfix: builtin load_module repeat expansion no longer references stale ref
# - bugfix: comma splitting now respects (), [] and {} so
#   print startswith("T-OS", "T-") works
# ============================================================

try:
    _NC_ORIG_BASE_ENV_EXT = NCInterpreter.base_env
except Exception:
    _NC_ORIG_BASE_ENV_EXT = None

try:
    _NC_ORIG_BUILTIN_MODULE_EXT = NCInterpreter._builtin_module
except Exception:
    _NC_ORIG_BUILTIN_MODULE_EXT = None


def _nc_ext_abs_path(interp, path):
    p = '' if path is None else str(path)
    if not os.path.isabs(p):
        p = os.path.join(interp._base_dir_current or '.', p)
    return p


def _nc_ext_typeof(value):
    if value is None:
        return 'none'
    if isinstance(value, bool):
        return 'bool'
    if isinstance(value, int) and not isinstance(value, bool):
        return 'int'
    if isinstance(value, float):
        return 'float'
    if isinstance(value, str):
        return 'str'
    if isinstance(value, list):
        return 'list'
    if isinstance(value, tuple):
        return 'tuple'
    if isinstance(value, dict):
        return 'dict'
    if isinstance(value, NCModule):
        return 'module'
    if isinstance(value, NCFn):
        return 'function'
    return type(value).__name__


def _nc_ext_contains(value, part):
    try:
        return part in value
    except Exception:
        return str(part) in str(value)


def _nc_ext_startswith(value, part):
    return str(value).startswith(str(part))


def _nc_ext_endswith(value, part):
    return str(value).endswith(str(part))


def _nc_ext_replace(value, old, new):
    return str(value).replace(str(old), str(new))


def _nc_ext_split(value, sep=None):
    if sep is None:
        return str(value).split()
    return str(value).split(str(sep))


def _nc_ext_join(items, sep=''):
    seq = items if isinstance(items, (list, tuple)) else [items]
    return str(sep).join('' if x is None else str(x) for x in seq)


def _nc_ext_keys(value):
    if isinstance(value, dict):
        return list(value.keys())
    try:
        return list(value.keys())
    except Exception:
        return []


def _nc_ext_values(value):
    if isinstance(value, dict):
        return list(value.values())
    try:
        return list(value.values())
    except Exception:
        return []


def _nc_ext_min(*args):
    vals = args[0] if len(args) == 1 and isinstance(args[0], (list, tuple)) else args
    vals = list(vals)
    return min(vals) if vals else None


def _nc_ext_max(*args):
    vals = args[0] if len(args) == 1 and isinstance(args[0], (list, tuple)) else args
    vals = list(vals)
    return max(vals) if vals else None


def _nc_ext_avg(values):
    arr = list(values) if isinstance(values, (list, tuple)) else []
    return (sum(arr) / len(arr)) if arr else 0


def _nc_ext_percent(part, total):
    try:
        total_f = float(total)
        if total_f == 0:
            return 0
        return (float(part) / total_f) * 100.0
    except Exception:
        return 0


def _nc_ext_clamp(value, mn, mx):
    try:
        return max(mn, min(mx, value))
    except Exception:
        return value


def _nc_ext_check_file(interp, name):
    safe = re.sub(r'[^A-Za-z0-9_.-]+', '_', str(name).strip()) or 'default'
    return os.path.join(interp._base_dir_current or '.', f'.nc_check_{safe}.txt')


def _nc_ext_check_set(interp, name, value):
    p = _nc_ext_check_file(interp, name)
    os.makedirs(os.path.dirname(p) or '.', exist_ok=True)
    with open(p, 'w', encoding='utf-8') as f:
        f.write('1' if _nc_to_bool(value) else '0')
    return _nc_to_bool(value)


def _nc_ext_check_get(interp, name, default=False):
    p = _nc_ext_check_file(interp, name)
    try:
        raw = open(p, 'r', encoding='utf-8').read().strip().lower()
        if raw in ('1', 'true', 'yes', 'on'):
            return True
        if raw in ('0', 'false', 'no', 'off'):
            return False
    except Exception:
        pass
    return _nc_to_bool(default)


def _nc_ext_check_toggle(interp, name):
    new_val = not _nc_ext_check_get(interp, name, False)
    _nc_ext_check_set(interp, name, new_val)
    return new_val


def _nc_ext_check_delete(interp, name):
    p = _nc_ext_check_file(interp, name)
    try:
        os.remove(p)
        return True
    except Exception:
        return False


def _nc_ext_file_module_object(self):
    @_nc_callable
    def read(path):
        p = _nc_ext_abs_path(self, path)
        with open(p, 'r', encoding='utf-8', errors='replace') as f:
            return f.read()

    @_nc_callable
    def write(path, value):
        p = _nc_ext_abs_path(self, path)
        os.makedirs(os.path.dirname(p) or '.', exist_ok=True)
        with open(p, 'w', encoding='utf-8') as f:
            f.write('' if value is None else str(value))
        return p

    @_nc_callable
    def append(path, value):
        p = _nc_ext_abs_path(self, path)
        os.makedirs(os.path.dirname(p) or '.', exist_ok=True)
        with open(p, 'a', encoding='utf-8') as f:
            f.write('' if value is None else str(value))
        return p

    @_nc_callable
    def exists(path):
        return os.path.exists(_nc_ext_abs_path(self, path))

    @_nc_callable
    def mkdir(path):
        p = _nc_ext_abs_path(self, path)
        os.makedirs(p, exist_ok=True)
        return p

    @_nc_callable
    def delete(path):
        p = _nc_ext_abs_path(self, path)
        try:
            if os.path.isdir(p):
                os.rmdir(p)
            else:
                os.remove(p)
            return True
        except Exception:
            return False

    @_nc_callable
    def listdir(path='.'):
        p = _nc_ext_abs_path(self, path)
        try:
            return os.listdir(p)
        except Exception:
            return []

    @_nc_callable
    def list_(path='.'):
        return listdir(path)

    @_nc_callable
    def size(path):
        p = _nc_ext_abs_path(self, path)
        try:
            return os.path.getsize(p)
        except Exception:
            return 0

    return _nc_make_simple_module(
        read=read, write=write, append=append, exists=exists, mkdir=mkdir,
        delete=delete, listdir=listdir, list=list_, size=size
    )


def _nc_ext_time_module_object(self):
    @_nc_callable
    def now_ms():
        return int(time.time() * 1000)

    @_nc_callable
    def now():
        return time.time()

    @_nc_callable
    def unix():
        return int(time.time())

    @_nc_callable
    def now_text():
        return time.strftime('%Y-%m-%d %H:%M:%S')

    @_nc_callable
    def now_iso():
        return time.strftime('%Y-%m-%dT%H:%M:%S')

    @_nc_callable
    def sleep_ms(ms):
        time.sleep(max(0.0, float(ms) / 1000.0))
        return True

    @_nc_callable
    def sleep(sec):
        time.sleep(max(0.0, float(sec)))
        return True

    @_nc_callable
    def sleep_sec(sec):
        return sleep(sec)

    return _nc_make_simple_module(
        now_ms=now_ms, now=now, unix=unix, now_text=now_text, now_iso=now_iso,
        sleep_ms=sleep_ms, sleep=sleep, sleep_sec=sleep_sec
    )


def _nc_ext_text_module_object(self):
    @_nc_callable
    def trim(s=''):
        return str(s).strip()

    @_nc_callable
    def strip(s=''):
        return str(s).strip()

    @_nc_callable
    def lower(s=''):
        return str(s).lower()

    @_nc_callable
    def upper(s=''):
        return str(s).upper()

    @_nc_callable
    def replace(s='', old='', new=''):
        return str(s).replace(str(old), str(new))

    @_nc_callable
    def contains(s='', part=''):
        return _nc_ext_contains(s, part)

    @_nc_callable
    def startswith(s='', part=''):
        return str(s).startswith(str(part))

    @_nc_callable
    def endswith(s='', part=''):
        return str(s).endswith(str(part))

    @_nc_callable
    def lines(s=''):
        return str(s).splitlines()

    @_nc_callable
    def count(s='', part=''):
        return str(s).count(str(part))

    @_nc_callable
    def split(s='', sep=None):
        return _nc_ext_split(s, sep)

    @_nc_callable
    def join(items=None, sep=''):
        return _nc_ext_join(items or [], sep)

    return _nc_make_simple_module(
        trim=trim, strip=strip, lower=lower, upper=upper, replace=replace,
        contains=contains, startswith=startswith, endswith=endswith,
        lines=lines, count=count, split=split, join=join
    )


def _nc_ext_array_module_object(self):
    @_nc_callable
    def first(arr=None):
        arr = list(arr) if isinstance(arr, (list, tuple)) else []
        return arr[0] if arr else None

    @_nc_callable
    def last(arr=None):
        arr = list(arr) if isinstance(arr, (list, tuple)) else []
        return arr[-1] if arr else None

    @_nc_callable
    def contains(arr=None, value=None):
        try:
            return value in (arr or [])
        except Exception:
            return False

    @_nc_callable
    def find(arr=None, value=None):
        try:
            return list(arr or []).index(value)
        except Exception:
            return -1

    @_nc_callable
    def slice(arr=None, start=None, end=None):
        return list(arr or [])[start:end]

    @_nc_callable
    def reverse(arr=None):
        return list(reversed(list(arr or [])))

    @_nc_callable
    def length(arr=None):
        return len(arr or [])

    @_nc_callable
    def unique(arr=None):
        return list(dict.fromkeys(list(arr or [])))

    @_nc_callable
    def push_(arr, value):
        return push(arr, value)

    @_nc_callable
    def pop_(arr):
        return pop(arr)

    return _nc_make_simple_module(
        first=first, last=last, contains=contains, find=find, slice=slice,
        reverse=reverse, length=length, len=length, unique=unique,
        push=push_, pop=pop_
    )


def _nc_ext_builtin_module(self, name):
    if _NC_ORIG_BUILTIN_MODULE_EXT is not None:
        base_mod = _NC_ORIG_BUILTIN_MODULE_EXT(self, name)
        if base_mod is not None:
            return base_mod

    # Larger native modules are kept in separate files and imported lazily.
    # This prevents physics or renderer dependencies from slowing down plain
    # console programs and gives every module an independent test boundary.
    try:
        from nc_module_registry import build_builtin_module

        native_module = build_builtin_module(str(name), self, NCModule)
        if native_module is not None:
            return native_module
    except ImportError as exc:
        # Compatibility with older portable folders that do not yet contain
        # the registry. Missing imports *inside* a registered module are real
        # dependency errors and must remain visible.
        if getattr(exc, "name", "") != "nc_module_registry":
            raise

    makers = {
        'file': self._file_module_object,
        'time': self._time_module_object,
        'text': self._text_module_object,
        'array': self._array_module_object,
    }
    if name not in makers:
        return None
    obj = makers[name]()
    mod = NCModule(name)
    exports = {}
    for k, v in obj.__dict__.items():
        if not str(k).startswith('_'):
            exports[k] = v
    mod.namespace = exports
    mod.exports = dict(exports)
    return mod


def _nc_ext_load_module(self, name, base, extra_paths=None, depth=0, caller_source='<text>', caller_line=1):
    if depth > self.policy.max_import_depth:
        raise NCError(_format_source(caller_source), caller_line, "NC blocked: import depth exceeded", self._import_stack)

    builtin = self._builtin_module(name)
    if builtin is not None:
        return builtin

    key = f"mod:{name}@{base}"
    if key in self.module_cache:
        return self.module_cache[key]

    candidates = self._module_search_paths(base, extra_paths)
    mod_text = None
    mod_ref = None

    for b in candidates:
        try:
            ref = _resolve_ref(b, name)
            if _is_url(ref):
                mod_text = _fetch_url_text(ref, self.policy)
            else:
                mod_text = _read_file_text(ref, self.policy)
            mod_ref = ref
            break
        except Exception:
            continue

    if mod_text is None:
        if name in BUILTIN_NC_MODULES:
            mod_ref = f"builtin:{name}"
            mod_text = BUILTIN_NC_MODULES[name]
        else:
            raise NCError(_format_source(caller_source), caller_line, f"NC import not found: {name}.nc", self._import_stack)

    module = NCModule(name)
    self.module_cache[key] = module

    parser = NCParser(mod_text, source_name=str(mod_ref))
    stmts = parser.parse()
    if parser.errors:
        err = NCMultiError(parser.errors, header="NC parse errors")
        raise err

    export_names = []
    stmts = _expand_repeat_program_top_level(stmts, str(mod_ref))

    env = self.base_env()
    env["__module__"] = module
    env["__exports__"] = export_names
    env["__export_base_keys__"] = set(env.keys())

    if _is_url(mod_ref or ''):
        base_dir = (mod_ref or '').rsplit('/', 1)[0] + '/'
    else:
        base_dir = os.path.dirname(mod_ref) if mod_ref and not _is_url(mod_ref) else base

    with self._with_source(str(mod_ref), push_stack=True):
        self.exec_block(
            stmts,
            env,
            base_dir=base_dir,
            extra_paths=extra_paths,
            in_module=True,
            source_name=str(mod_ref),
        )

    module.namespace = {k: v for k, v in env.items() if not k.startswith("__")}
    module.finalize_exports(export_names)
    return module


def _nc_ext_base_env(self):
    env = _NC_ORIG_BASE_ENV_EXT(self) if _NC_ORIG_BASE_ENV_EXT is not None else {}

    env.update({
        'str': _nc_callable(lambda value='': '' if value is None else str(value)),
        'to_str': _nc_callable(lambda value='': '' if value is None else str(value)),
        'float': _nc_callable(lambda value=0.0: float(str(value).strip().replace(',', '.'))),
        'to_float': _nc_callable(lambda value=0.0, default=None: (lambda s: float(s) if re.fullmatch(r'-?\d+(?:\.\d+)?', s) else default)(str(value).strip().replace(',', '.'))),
        'bool': _nc_callable(lambda value=None: _nc_to_bool(value)),
        'to_bool': _nc_callable(lambda value=None: _nc_to_bool(value)),
        'round': _nc_callable(lambda value=0, ndigits=0: round(value, int(ndigits))),
        'abs': _nc_callable(lambda value=0: abs(value)),
        'min': _nc_callable(_nc_ext_min),
        'max': _nc_callable(_nc_ext_max),
        'contains': _nc_callable(_nc_ext_contains),
        'startswith': _nc_callable(_nc_ext_startswith),
        'endswith': _nc_callable(_nc_ext_endswith),
        'replace': _nc_callable(_nc_ext_replace),
        'split': _nc_callable(_nc_ext_split),
        'join': _nc_callable(_nc_ext_join),
        'keys': _nc_callable(_nc_ext_keys),
        'values': _nc_callable(_nc_ext_values),
        'typeof': _nc_callable(_nc_ext_typeof),
        'length': _nc_callable(lambda value=None: len(value) if value is not None else 0),
        'unique': _nc_callable(lambda arr=None: list(dict.fromkeys(list(arr or [])))),
        'now': _nc_callable(lambda: time.time()),
        'now_text': _nc_callable(lambda: time.strftime('%Y-%m-%d %H:%M:%S')),
        'sleep_sec': _nc_callable(lambda sec: (time.sleep(max(0.0, float(sec))), True)[1]),
        'clamp': _nc_callable(_nc_ext_clamp),
        'avg': _nc_callable(_nc_ext_avg),
        'percent': _nc_callable(_nc_ext_percent),
        'check_set': _nc_callable(lambda name, value: _nc_ext_check_set(self, name, value)),
        'check_get': _nc_callable(lambda name, default=False: _nc_ext_check_get(self, name, default)),
        'check_toggle': _nc_callable(lambda name: _nc_ext_check_toggle(self, name)),
        'check_delete': _nc_callable(lambda name: _nc_ext_check_delete(self, name)),
        'true': True,
        'false': False,
    })

    env['file'] = self._file_module_object()
    env['time'] = self._time_module_object()
    env['text'] = self._text_module_object()
    env['array'] = self._array_module_object()
    return env


def _nc_ext_split_commas(s: str) -> list:
    out, buf, q = [], [], None
    paren = 0
    bracket = 0
    brace = 0
    for ch in s:
        if q:
            buf.append(ch)
            if ch == q:
                q = None
            continue
        if ch in ("'", '"'):
            q = ch
            buf.append(ch)
            continue
        if ch == '(':
            paren += 1
            buf.append(ch)
            continue
        if ch == ')':
            paren = max(0, paren - 1)
            buf.append(ch)
            continue
        if ch == '[':
            bracket += 1
            buf.append(ch)
            continue
        if ch == ']':
            bracket = max(0, bracket - 1)
            buf.append(ch)
            continue
        if ch == '{':
            brace += 1
            buf.append(ch)
            continue
        if ch == '}':
            brace = max(0, brace - 1)
            buf.append(ch)
            continue
        if ch == ',' and paren == 0 and bracket == 0 and brace == 0:
            part = ''.join(buf).strip()
            if part:
                out.append(part)
            buf = []
        else:
            buf.append(ch)
    part = ''.join(buf).strip()
    if part:
        out.append(part)
    return out


# ============================================================
# NCE packages (NC encrypted archives)
# ============================================================
# .nce is intentionally not a renamed ZIP. It stores a tar stream in memory,
# compresses it losslessly, then encrypts it with AES-GCM.

_NCE_MAGIC = b"NCENCRYPTED1\n"
_NCE_DEFAULT_ITERATIONS = 390_000
_NCE_CACHE: Dict[str, Dict[str, Any]] = {}

try:
    NCInterpreter._builtin_module = _nc_ext_builtin_module
    NCInterpreter.load_module = _nc_ext_load_module
    NCInterpreter.base_env = _nc_ext_base_env
except Exception:
    pass

_NCE_ORIG_READ_FILE_TEXT = _read_file_text
_NCE_ORIG_RESOLVE_REF = _resolve_ref
_NCE_ORIG_RUN_FILE = run_file
_NCE_ORIG_LOAD_FROM = NCInterpreter.load_from
_NCE_ORIG_BASE_ENV = NCInterpreter.base_env


def _nce_require_crypto():
    try:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    except Exception as e:
        raise RuntimeError("NCE needs the Python package 'cryptography'. Install: pip install cryptography") from e
    return hashes, AESGCM, PBKDF2HMAC


def _nce_password(password: Optional[str] = None, *, prompt: bool = False, confirm: bool = False) -> str:
    if password is None:
        password = os.environ.get("NC_NCE_PASSWORD")
    if password is None and prompt:
        import getpass
        first = getpass.getpass("NCE password: ")
        if confirm:
            second = getpass.getpass("Repeat NCE password: ")
            if first != second:
                raise ValueError("NCE passwords do not match")
        password = first
    password = "" if password is None else str(password)
    if not password:
        raise ValueError("NCE password missing. Use --nce-password or set NC_NCE_PASSWORD.")
    if len(password) < 8:
        raise ValueError("NCE password must be at least 8 characters long")
    return password


def _nce_key(password: str, salt: bytes, iterations: int) -> bytes:
    hashes, _AESGCM, PBKDF2HMAC = _nce_require_crypto()
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=int(iterations),
    )
    return kdf.derive(password.encode("utf-8"))


def _nce_norm_inner(path: str) -> str:
    p = str(path or "").replace("\\", "/").strip()
    p = p[2:] if p.startswith("./") else p
    p = p.lstrip("/")
    parts = []
    for part in p.split("/"):
        if not part or part == ".":
            continue
        if part == "..":
            raise ValueError(f"Blocked unsafe NCE path: {path}")
        parts.append(part)
    return "/".join(parts)


def _nce_join_inner(base_inner: str, target: str) -> str:
    target = str(target or "").replace("\\", "/")
    if target.startswith("/"):
        out = target.lstrip("/")
    else:
        b = str(base_inner or "").replace("\\", "/").strip("/")
        out = (b + "/" + target) if b else target
    if not out.lower().endswith(".nc"):
        out += ".nc"
    return _nce_norm_inner(out)


def _nce_make_ref(package_path: str, inner: str = "") -> str:
    return "nce:" + os.path.abspath(package_path) + "!/" + _nce_norm_inner(inner)


def _nce_is_ref(ref: str) -> bool:
    return str(ref or "").startswith("nce:") and "!/" in str(ref or "")


def _nce_parse_ref(ref: str) -> Tuple[str, str]:
    s = str(ref or "")
    if not _nce_is_ref(s):
        raise ValueError(f"Not an NCE ref: {ref}")
    package, inner = s[4:].split("!/", 1)
    return os.path.abspath(package), _nce_norm_inner(inner)


def _nce_find_entry(files: Dict[str, bytes], requested: Optional[str]) -> str:
    if requested:
        req = _nce_norm_inner(requested)
        if req in files and req.lower().endswith(".nc"):
            return req
        raise FileNotFoundError(f"NCE entrypoint not found: {requested}")
    for name in ("main.nc", "index.nc", "app.nc"):
        if name in files:
            return name
    nc_files = sorted(p for p in files if p.lower().endswith(".nc"))
    if not nc_files:
        raise ValueError("NCE package contains no .nc file")
    return nc_files[0]


def create_nce(source: str, output: str, password: Optional[str] = None, entrypoint: Optional[str] = None) -> str:
    import tarfile
    import zlib
    from io import BytesIO

    source_abs = os.path.abspath(source)
    output_abs = os.path.abspath(output)
    password = _nce_password(password, prompt=True, confirm=True)
    if not os.path.exists(source_abs):
        raise FileNotFoundError(source_abs)

    files_for_entry: Dict[str, bytes] = {}
    tar_buf = BytesIO()
    with tarfile.open(fileobj=tar_buf, mode="w") as tar:
        def add_file(full_path: str, arcname: str) -> None:
            arc = _nce_norm_inner(arcname)
            if not arc:
                return
            if os.path.abspath(full_path) == output_abs:
                return
            if os.path.islink(full_path) or not os.path.isfile(full_path):
                return
            with open(full_path, "rb") as f:
                data = f.read()
            info = tarfile.TarInfo(arc)
            info.size = len(data)
            info.mtime = int(os.path.getmtime(full_path))
            info.mode = 0o644
            tar.addfile(info, BytesIO(data))
            if arc.lower().endswith(".nc"):
                files_for_entry[arc] = data

        if os.path.isfile(source_abs):
            add_file(source_abs, os.path.basename(source_abs))
        else:
            for root, dirs, names in os.walk(source_abs):
                dirs[:] = [d for d in dirs if d not in ("__pycache__", ".git", "nc_exe_build")]
                for name in names:
                    full = os.path.join(root, name)
                    arc = os.path.relpath(full, source_abs)
                    add_file(full, arc)

        selected_entry = _nce_find_entry(files_for_entry, entrypoint)
        manifest = {
            "format": "nce",
            "version": 1,
            "entrypoint": selected_entry,
            "created": int(time.time()),
        }
        manifest_data = json.dumps(manifest, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        info = tarfile.TarInfo(".nce_manifest.json")
        info.size = len(manifest_data)
        info.mtime = int(time.time())
        info.mode = 0o600
        tar.addfile(info, BytesIO(manifest_data))

    plain = zlib.compress(tar_buf.getvalue(), level=9)
    _hashes, AESGCM, _PBKDF2HMAC = _nce_require_crypto()
    salt = secrets.token_bytes(16)
    nonce = secrets.token_bytes(12)
    header = {
        "version": 1,
        "cipher": "AES-256-GCM",
        "kdf": "PBKDF2-HMAC-SHA256",
        "iterations": _NCE_DEFAULT_ITERATIONS,
        "salt": salt.hex(),
        "nonce": nonce.hex(),
        "compression": "zlib",
    }
    header_bytes = json.dumps(header, sort_keys=True, separators=(",", ":")).encode("utf-8")
    key = _nce_key(password, salt, _NCE_DEFAULT_ITERATIONS)
    ciphertext = AESGCM(key).encrypt(nonce, plain, _NCE_MAGIC + header_bytes)
    with open(output_abs, "wb") as f:
        f.write(_NCE_MAGIC)
        f.write(len(header_bytes).to_bytes(4, "big"))
        f.write(header_bytes)
        f.write(ciphertext)
    _NCE_CACHE.pop(output_abs, None)
    return output_abs


def _nce_load_package(path: str, policy: Optional[NCPolicy] = None, password: Optional[str] = None) -> Dict[str, Any]:
    import tarfile
    import zlib
    from io import BytesIO

    policy = policy or NCPolicy()
    abs_path = os.path.abspath(path)
    st = os.stat(abs_path)
    password = _nce_password(password, prompt=bool(getattr(sys.stdin, "isatty", lambda: False)()))
    cache_key = abs_path + ":" + hashlib.sha256(password.encode("utf-8")).hexdigest()
    cached = _NCE_CACHE.get(cache_key)
    if cached and cached.get("mtime") == st.st_mtime and cached.get("size") == st.st_size:
        return cached

    max_bytes = max(int(getattr(policy, "max_module_bytes", 300_000)) * 20, 50_000_000)
    if st.st_size > max_bytes:
        raise ValueError(f"Blocked: NCE package too large: {abs_path}")
    with open(abs_path, "rb") as f:
        data = f.read()
    if not data.startswith(_NCE_MAGIC):
        raise ValueError(f"Not an NCE package: {abs_path}")
    pos = len(_NCE_MAGIC)
    header_len = int.from_bytes(data[pos:pos + 4], "big")
    pos += 4
    if header_len <= 0 or header_len > 64_000:
        raise ValueError("Invalid NCE header")
    header_bytes = data[pos:pos + header_len]
    pos += header_len
    header = json.loads(header_bytes.decode("utf-8"))
    salt = bytes.fromhex(str(header.get("salt", "")))
    nonce = bytes.fromhex(str(header.get("nonce", "")))
    iterations = int(header.get("iterations", _NCE_DEFAULT_ITERATIONS))
    _hashes, AESGCM, _PBKDF2HMAC = _nce_require_crypto()
    key = _nce_key(password, salt, iterations)
    try:
        compressed = AESGCM(key).decrypt(nonce, data[pos:], _NCE_MAGIC + header_bytes)
    except Exception as e:
        raise ValueError("NCE decrypt failed: wrong password or damaged package") from e
    tar_data = zlib.decompress(compressed)

    files: Dict[str, bytes] = {}
    with tarfile.open(fileobj=BytesIO(tar_data), mode="r:") as tar:
        for member in tar.getmembers():
            inner = _nce_norm_inner(member.name)
            if not inner or not member.isfile():
                continue
            extracted = tar.extractfile(member)
            if extracted is None:
                continue
            files[inner] = extracted.read()

    manifest = {}
    if ".nce_manifest.json" in files:
        try:
            manifest = json.loads(files[".nce_manifest.json"].decode("utf-8", errors="replace"))
        except Exception:
            manifest = {}
    entrypoint = _nce_find_entry(files, manifest.get("entrypoint"))
    loaded = {
        "path": abs_path,
        "mtime": st.st_mtime,
        "size": st.st_size,
        "files": files,
        "entrypoint": entrypoint,
        "manifest": manifest,
    }
    _NCE_CACHE[cache_key] = loaded
    return loaded


def nce_read_entry_text(path: str, policy: Optional[NCPolicy] = None, password: Optional[str] = None) -> Tuple[str, str, str]:
    package = _nce_load_package(path, policy=policy, password=password)
    entry = package["entrypoint"]
    data = package["files"][entry]
    if len(data) > int((policy or NCPolicy()).max_module_bytes):
        raise ValueError(f"Blocked: module too large inside NCE: {entry}")
    ref = _nce_make_ref(package["path"], entry)
    base = _nce_make_ref(package["path"], os.path.dirname(entry))
    return data.decode("utf-8", errors="replace"), base, ref


def nce_list_files(path: str, policy: Optional[NCPolicy] = None, password: Optional[str] = None) -> List[str]:
    package = _nce_load_package(path, policy=policy, password=password)
    return sorted(k for k in package["files"].keys() if k != ".nce_manifest.json")


def _nce_read_file_text(path: str, policy: NCPolicy) -> str:
    if _nce_is_ref(path):
        package_path, inner = _nce_parse_ref(path)
        package = _nce_load_package(package_path, policy=policy)
        data = package["files"].get(inner)
        if data is None:
            raise FileNotFoundError(path)
        if len(data) > policy.max_module_bytes:
            raise ValueError(f"Blocked: module too large inside NCE: {inner}")
        return data.decode("utf-8", errors="replace")
    return _NCE_ORIG_READ_FILE_TEXT(path, policy)


def _nce_resolve_ref(base: str, name_or_path: str) -> str:
    base_s = str(base or "")
    target = str(name_or_path or "")
    if _nce_is_ref(base_s):
        package_path, inner_base = _nce_parse_ref(base_s)
        inner = _nce_join_inner(inner_base, target)
        return _nce_make_ref(package_path, inner)
    return _NCE_ORIG_RESOLVE_REF(base, name_or_path)


def _nce_load_from(self, src: str, name: str, base: str, extra_paths=None, depth: int = 0, caller_source: str = "<text>", caller_line: int = 1):
    if not _nce_is_ref(base):
        return _NCE_ORIG_LOAD_FROM(self, src, name, base, extra_paths, depth, caller_source, caller_line)
    if depth > self.policy.max_import_depth:
        raise NCError(_format_source(caller_source), caller_line, "NC blocked: import depth exceeded", self._import_stack)
    package_path, inner_base = _nce_parse_ref(base)
    src_inner = _nce_norm_inner((inner_base.rstrip("/") + "/" + src).strip("/"))
    ref = _nce_make_ref(package_path, _nce_join_inner(src_inner, name))
    key = f"from:{ref}"
    if key in self.module_cache:
        return self.module_cache[key]
    text = _nce_read_file_text(ref, self.policy)
    module = NCModule(name)
    self.module_cache[key] = module
    parser = NCParser(text, source_name=str(ref))
    stmts = parser.parse()
    if parser.errors:
        err = NCMultiError(parser.errors, header="NC parse errors")
        raise err
    export_names: List[str] = []
    env = self.base_env()
    env["__module__"] = module
    env["__exports__"] = export_names
    env["__export_base_keys__"] = set(env.keys())
    new_base = _nce_make_ref(package_path, os.path.dirname(_nce_parse_ref(ref)[1]))
    with self._with_source(str(ref), push_stack=True):
        self.exec_block(stmts, env, base_dir=new_base, extra_paths=extra_paths, in_module=True, source_name=str(ref))
    module.namespace = {k: v for k, v in env.items() if not k.startswith("__")}
    module.finalize_exports(export_names)
    return module


def _nce_run_file(path: str, policy=None, enable_ui: bool = True, extra_paths=None):
    policy = policy or NCPolicy()
    if str(path).lower().endswith(".nce"):
        text, base, source_name = nce_read_entry_text(path, policy=policy)
        return run_text(text, base=base, extra_paths=extra_paths, policy=policy, enable_ui=enable_ui, source_name=source_name)
    return _NCE_ORIG_RUN_FILE(path, policy=policy, enable_ui=enable_ui, extra_paths=extra_paths)


def _nce_base_env(self):
    env = _NCE_ORIG_BASE_ENV(self)

    class _NCEMod:
        pass

    mod = _NCEMod()

    @_nc_callable
    def list_files(path):
        return nce_list_files(str(path), policy=self.policy)

    @_nc_callable
    def entry(path):
        package = _nce_load_package(str(path), policy=self.policy)
        return str(package.get("entrypoint") or "")

    mod.list_files = list_files
    mod.entry = entry
    env["nce"] = mod
    return env


_read_file_text = _nce_read_file_text
_resolve_ref = _nce_resolve_ref
NCInterpreter.load_from = _nce_load_from
NCInterpreter.base_env = _nce_base_env
run_file = _nce_run_file
