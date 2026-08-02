"""Structured, source-aware diagnostics for the NC parser and runtime."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any, Iterable


_SOURCE_CACHE: dict[str, list[str]] = {}
_SOURCE_CACHE_LIMIT = 128


def register_source(source: Any, text: Any) -> None:
    key = str(source or "<text>")
    if len(_SOURCE_CACHE) >= _SOURCE_CACHE_LIMIT and key not in _SOURCE_CACHE:
        oldest = next(iter(_SOURCE_CACHE))
        _SOURCE_CACHE.pop(oldest, None)
    _SOURCE_CACHE[key] = str(text or "").splitlines()


def source_line(source: Any, line: int) -> str:
    key = str(source or "<text>")
    lines = _SOURCE_CACHE.get(key)
    if lines is None and os.path.isfile(key):
        try:
            with open(key, "r", encoding="utf-8", errors="replace") as handle:
                lines = handle.read().splitlines()
            _SOURCE_CACHE[key] = lines
        except OSError:
            lines = None
    index = max(1, int(line)) - 1
    if lines is None or index >= len(lines):
        return ""
    return lines[index]


@dataclass
class RelatedDiagnostic:
    source: str
    line: int
    column: int
    message: str


@dataclass
class Diagnostic:
    code: str
    title: str
    message: str
    source: str
    line: int
    column: int = 1
    end_column: int | None = None
    help: str | None = None
    label: str | None = None
    severity: str = "error"
    related: list[RelatedDiagnostic] = field(default_factory=list)
    import_stack: list[str] = field(default_factory=list)
    call_stack: list[dict[str, Any]] = field(default_factory=list)


def _first_non_space(line_text: str) -> int:
    return len(line_text) - len(line_text.lstrip(" ")) + 1


def _syntax_keyword(line_text: str) -> str:
    stripped = line_text.strip()
    return stripped.split(None, 1)[0] if stripped else "block"


def _classify(message: str, source: str, line: int) -> Diagnostic:
    original = str(message or "Unknown NC error").strip()
    text = source_line(source, line)
    lowered = original.lower()
    column = _first_non_space(text)
    end_column: int | None = None
    title = "Runtime error"
    code = "NC-R4000"
    help_text: str | None = None
    label: str | None = None
    cleaned = original

    missing_colon = "missing ':'" in lowered or "missing ':' at the end" in lowered
    if missing_colon:
        keyword = _syntax_keyword(text)
        code = "NC-S1001"
        title = f"Missing ':' after {keyword} block header"
        column = len(text.rstrip()) + 1
        end_column = column
        label = "expected ':'"
        cleaned = "This block header is incomplete."
        proposed = text.rstrip() + ":" if text.strip() else "add ':' to the block header"
        help_text = f"Write `{proposed.strip()}`"
    elif "unexpected indent" in lowered:
        code = "NC-S1002"
        title = "Unexpected indentation"
        label = "this line is indented more than its current block"
        help_text = "Use exactly 2 spaces per NC block level, or fix the preceding block header."
    elif "tabs not allowed" in lowered:
        code = "NC-S1003"
        title = "Tab indentation is not allowed"
        tab_index = text.find("\t")
        column = tab_index + 1 if tab_index >= 0 else 1
        end_column = column + 1
        label = "replace this tab"
        help_text = "Replace each indentation tab with spaces; NC uses 2 spaces per block level."
    elif "indent must be multiple of 2" in lowered:
        code = "NC-S1004"
        title = "Invalid indentation width"
        label = "indentation is not a multiple of 2 spaces"
        help_text = "Use 0, 2, 4, 6, ... leading spaces."
    elif "bad fn statement" in lowered:
        code = "NC-S1010"
        title = "Invalid function declaration"
        label = "expected `fn name(arguments):`"
        help_text = "Example: `fn move(x, y):`"
    elif "unknown assignment syntax" in lowered:
        code = "NC-S1011"
        title = "Assignment needs 'let' or 'set'"
        label = "assignment starts here"
        match = re.search(r"'([^']+) = \.\.\.'", original)
        name = match.group(1) if match else "value"
        help_text = f"Use `let {name} = ...` for a new variable or `set {name} = ...` to update it."
    elif "bad expression" in lowered or "invalid syntax" in lowered:
        code = "NC-S1101"
        title = "Invalid expression"
        label = "NC could not parse this expression"
        cleaned = re.sub(r"^Bad expression:\s*", "", original, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*\(<unknown>, line 1\)\s*$", "", cleaned)
        help_text = "Check quotes, brackets, operators, and commas in this expression."
    elif "unknown name:" in lowered:
        code = "NC-N2001"
        title = "Unknown name"
        match = re.search(r"Unknown name:\s*([A-Za-z_]\w*)", original, flags=re.IGNORECASE)
        name = match.group(1) if match else "name"
        name_index = text.find(name)
        if name_index >= 0:
            column = name_index + 1
            end_column = column + len(name)
        label = f"`{name}` is not defined in this scope"
        suggestion = re.search(r"Did you mean:\s*(.+?)\??$", original)
        help_text = (
            f"Did you mean {suggestion.group(1).strip()}?"
            if suggestion
            else f"Define `{name}` with `let`, import it, or correct its spelling."
        )
        cleaned = f"NC cannot find `{name}` in the current scope."
    elif "import not found" in lowered:
        code = "NC-M3001"
        title = "Module not found"
        label = "this import could not be resolved"
        help_text = "Check the module name and the project, libs, and standart_imports search paths."
    elif "has no export" in lowered:
        code = "NC-M3002"
        title = "Module export not found"
        help_text = "Export the name from the module or use one of its public exports."
    elif "import depth exceeded" in lowered:
        code = "NC-M3003"
        title = "Import nesting limit exceeded"
        help_text = "Check for modules that import each other in a cycle."
    elif "execution step limit exceeded" in lowered:
        code = "NC-R4001"
        title = "Execution step limit exceeded"
        help_text = "Check for an endless loop or increase max_steps only for a deliberately large program."
    elif "division by zero" in lowered or "zerodivisionerror" in lowered:
        code = "NC-R4010"
        title = "Division by zero"
        help_text = "Ensure the divisor is not zero before performing the division."
    elif "must be" in lowered or "expects" in lowered:
        code = "NC-T4100"
        title = "Invalid value or type"
    elif "needs pyside6" in lowered or "needs panda3d" in lowered or "dependency" in lowered:
        code = "NC-D5001"
        title = "Required runtime component is missing"
        help_text = "Run the NC installer again to install the declared runtime dependencies."
    elif "file not found" in lowered or "unsupported image format" in lowered or "unsupported 3d model" in lowered:
        code = "NC-D5100"
        title = "Resource could not be loaded"
    elif "blocked" in lowered:
        code = "NC-P6001"
        title = "Operation blocked by NC policy"

    if end_column is None:
        end_column = max(column + 1, len(text.rstrip()) + 1) if text else column + 1
    return Diagnostic(
        code=code,
        title=title,
        message=cleaned,
        source=str(source or "<text>"),
        line=max(1, int(line)),
        column=max(1, int(column)),
        end_column=max(1, int(end_column)),
        help=help_text,
        label=label,
    )


def diagnostic_from_error(error: Any) -> Diagnostic:
    diagnostic = _classify(
        str(getattr(error, "message", error)),
        str(getattr(error, "source", "<text>")),
        int(getattr(error, "line", 1) or 1),
    )
    explicit_code = getattr(error, "code", None)
    if explicit_code:
        diagnostic.code = str(explicit_code)
    explicit_help = getattr(error, "help", None)
    if explicit_help:
        diagnostic.help = str(explicit_help)
    diagnostic.import_stack = list(getattr(error, "import_stack", []) or [])
    diagnostic.call_stack = list(getattr(error, "call_stack", []) or [])
    return diagnostic


def relate_diagnostics(diagnostics: list[Diagnostic]) -> list[Diagnostic]:
    missing_headers = [diagnostic for diagnostic in diagnostics if diagnostic.code == "NC-S1001"]
    for diagnostic in diagnostics:
        if diagnostic.code != "NC-S1002":
            continue
        candidates = [
            header
            for header in missing_headers
            if header.source == diagnostic.source and 0 < diagnostic.line - header.line <= 2
        ]
        if not candidates:
            continue
        cause = max(candidates, key=lambda item: item.line)
        cause.related.append(
            RelatedDiagnostic(
                source=diagnostic.source,
                line=diagnostic.line,
                column=diagnostic.column,
                message="This indentation diagnostic is a consequence of the missing ':'.",
            )
        )
        diagnostic.severity = "note"
        diagnostic.message = f"Caused by {cause.code} on line {cause.line}."
        diagnostic.help = None
    return diagnostics


def _location_width(diagnostic: Diagnostic) -> int:
    values = [diagnostic.line] + [related.line for related in diagnostic.related]
    return max(1, len(str(max(values))))


def _marker(column: int, end_column: int | None, label: str | None) -> str:
    start = max(1, column)
    end = max(start + 1, end_column or start + 1)
    carets = "^" * max(1, end - start)
    return " " * (start - 1) + carets + (f" {label}" if label else "")


def format_diagnostic(diagnostic: Diagnostic) -> str:
    width = _location_width(diagnostic)
    lines = [
        f"{diagnostic.severity} {diagnostic.code}: {diagnostic.title}",
        f"  --> {diagnostic.source}:{diagnostic.line}:{diagnostic.column}",
        " " * (width + 1) + "|",
    ]
    text = source_line(diagnostic.source, diagnostic.line)
    if text:
        lines.append(f"{diagnostic.line:>{width}} | {text}")
        lines.append(" " * (width + 1) + "| " + _marker(diagnostic.column, diagnostic.end_column, diagnostic.label))
    if diagnostic.message and diagnostic.message != diagnostic.title:
        lines.append(" " * (width + 1) + f"= {diagnostic.message}")
    for related in diagnostic.related:
        related_text = source_line(related.source, related.line)
        lines.append(" " * (width + 1) + "|")
        lines.append(f"{related.line:>{width}} | {related_text}")
        lines.append(
            " " * (width + 1)
            + "| "
            + _marker(related.column, related.column + 1, related.message)
        )
    if diagnostic.help:
        lines.append(" " * (width + 1) + f"= help: {diagnostic.help}")
    if diagnostic.call_stack:
        lines.append(" " * (width + 1) + "= NC call stack:")
        for frame in reversed(diagnostic.call_stack):
            lines.append(
                " " * (width + 3)
                + f"at {frame.get('name', '<function>')} "
                + f"({frame.get('source', '<text>')}:{frame.get('line', 1)})"
            )
    if diagnostic.import_stack:
        lines.append(" " * (width + 1) + "= import chain: " + " -> ".join(diagnostic.import_stack))
    return "\n".join(lines)


def format_errors(errors: Iterable[Any], header: str | None = None) -> str:
    diagnostics = relate_diagnostics([diagnostic_from_error(error) for error in errors])
    rendered = [format_diagnostic(diagnostic) for diagnostic in diagnostics]
    if header and len(rendered) > 1:
        return f"{header} ({len(rendered)} diagnostics)\n\n" + "\n\n".join(rendered)
    return "\n\n".join(rendered)


def format_exception(error: BaseException) -> str:
    errors = getattr(error, "errors", None)
    if isinstance(errors, list):
        return format_errors(errors, str(error))
    if hasattr(error, "source") and hasattr(error, "line"):
        return format_diagnostic(diagnostic_from_error(error))
    diagnostic = _classify(str(error), "<runtime>", 1)
    return format_diagnostic(diagnostic)
