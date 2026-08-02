"""Shared runtime helpers for native NC standard-library modules.

This module deliberately has no dependency on :mod:`nc`.  Keeping the helper
layer independent prevents circular imports while the interpreter is creating
its builtin module objects.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence


class NCRuntimeModuleError(RuntimeError):
    """Base exception for errors raised by native NC modules."""


class NCConfigurationError(NCRuntimeModuleError):
    """Raised when an NC module receives an invalid option."""


class NCDependencyError(NCRuntimeModuleError):
    """Raised when an optional graphical dependency is unavailable."""


class NCResourceError(NCRuntimeModuleError):
    """Raised when an image, model, or other resource cannot be loaded."""


def nc_callable(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Mark a Python callable as intentionally exposed to NC code."""

    setattr(fn, "__nc_callable__", True)
    return fn


def invoke_nc_callback(callback: Any, *args: Any) -> Any:
    """Invoke either an ``NCFn`` or an explicitly exposed Python callable."""

    if callback is None:
        return None
    call_method = getattr(callback, "call", None)
    if callable(call_method):
        declared = getattr(callback, "arg_names", None)
        if isinstance(declared, (list, tuple)):
            # Event callbacks may ignore details they do not need. This keeps
            # zero-argument handlers compatible while still validating that a
            # handler did not request more values than the event provides.
            if len(declared) > len(args):
                raise NCConfigurationError(
                    f"Callback expects {len(declared)} arguments, but this event provides {len(args)}"
                )
            return call_method(list(args[: len(declared)]))
        return call_method(list(args))
    if callable(callback):
        return callback(*args)
    raise NCConfigurationError("Callback must be an NC function")


def finite_number(value: Any, name: str) -> float:
    """Convert a value to a finite float and report the option name on error."""

    try:
        number = float(value)
    except (TypeError, ValueError):
        raise NCConfigurationError(f"{name} must be a number, got {value!r}") from None
    if not math.isfinite(number):
        raise NCConfigurationError(f"{name} must be finite, got {value!r}")
    return number


def positive_number(value: Any, name: str, *, allow_zero: bool = False) -> float:
    number = finite_number(value, name)
    if number < 0.0 or (number == 0.0 and not allow_zero):
        relation = "zero or greater" if allow_zero else "greater than zero"
        raise NCConfigurationError(f"{name} must be {relation}, got {number}")
    return number


def integer_at_least(value: Any, name: str, minimum: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise NCConfigurationError(f"{name} must be an integer, got {value!r}") from None
    if number < minimum:
        raise NCConfigurationError(f"{name} must be at least {minimum}, got {number}")
    return number


def options_dict(value: Any, name: str = "options") -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise NCConfigurationError(f"{name} must be a dictionary")
    return dict(value)


def vector(value: Any, dimensions: int, name: str) -> list[float]:
    if not isinstance(value, (list, tuple)) or len(value) != dimensions:
        raise NCConfigurationError(
            f"{name} must contain exactly {dimensions} numbers, got {value!r}"
        )
    return [finite_number(component, f"{name}[{index}]") for index, component in enumerate(value)]


def optional_vector(value: Any, dimensions: int, name: str, default: Sequence[float]) -> list[float]:
    return list(default) if value is None else vector(value, dimensions, name)


def clamp(value: float, low: float, high: float) -> float:
    return low if value < low else high if value > high else value


def dot(a: Sequence[float], b: Sequence[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


def length_squared(v: Sequence[float]) -> float:
    return dot(v, v)


def length(v: Sequence[float]) -> float:
    return math.sqrt(length_squared(v))


def normalized(v: Sequence[float], fallback: Sequence[float] | None = None) -> list[float]:
    magnitude = length(v)
    if magnitude <= 1e-12:
        if fallback is None:
            return [0.0 for _ in v]
        return list(fallback)
    return [component / magnitude for component in v]


def add(a: Sequence[float], b: Sequence[float]) -> list[float]:
    return [x + y for x, y in zip(a, b)]


def subtract(a: Sequence[float], b: Sequence[float]) -> list[float]:
    return [x - y for x, y in zip(a, b)]


def scale(v: Sequence[float], factor: float) -> list[float]:
    return [component * factor for component in v]


def cross3(a: Sequence[float], b: Sequence[float]) -> list[float]:
    return [
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    ]


def resolve_resource_path(path: Any, base_dir: str, allowed_extensions: Iterable[str], kind: str) -> str:
    raw = str(path or "").strip()
    if not raw:
        raise NCResourceError(f"{kind} path cannot be empty")
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = Path(base_dir or ".") / candidate
    candidate = candidate.resolve()
    allowed = {str(ext).lower() for ext in allowed_extensions}
    if candidate.suffix.lower() not in allowed:
        joined = ", ".join(sorted(allowed))
        raise NCResourceError(
            f"Unsupported {kind} format '{candidate.suffix or '<none>'}'. Supported: {joined}"
        )
    if not candidate.is_file():
        raise NCResourceError(f"{kind.capitalize()} file not found: {candidate}")
    return os.fspath(candidate)


@dataclass(frozen=True)
class ImageAsset:
    path: str
    width_pixels: int | None = None
    height_pixels: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": "image",
            "path": self.path,
            "width_pixels": self.width_pixels,
            "height_pixels": self.height_pixels,
        }


@dataclass(frozen=True)
class ModelAsset:
    path: str
    format: str

    def to_dict(self) -> dict[str, Any]:
        return {"kind": "model", "path": self.path, "format": self.format}


def load_image_asset(path: Any, base_dir: str) -> ImageAsset:
    resolved = resolve_resource_path(
        path,
        base_dir,
        {".png", ".jpg", ".jpeg", ".webp", ".svg"},
        "image",
    )
    width: int | None = None
    height: int | None = None
    if Path(resolved).suffix.lower() != ".svg":
        try:
            from PIL import Image

            with Image.open(resolved) as image:
                width, height = image.size
        except Exception:
            # Pixel dimensions are metadata only; Qt can still attempt to load
            # the image at render time and report a renderer-specific error.
            width = None
            height = None
    return ImageAsset(resolved, width, height)


def load_model_asset(path: Any, base_dir: str) -> ModelAsset:
    resolved = resolve_resource_path(path, base_dir, {".glb", ".gltf", ".obj"}, "3D model")
    return ModelAsset(resolved, Path(resolved).suffix.lower().lstrip("."))


class IdentifierPool:
    def __init__(self, prefix: str):
        self._prefix = str(prefix)
        self._next = 1

    def allocate(self) -> str:
        value = f"{self._prefix}{self._next}"
        self._next += 1
        return value


class FixedStepClock:
    """Deterministic accumulator used only by real-time render loops."""

    def __init__(self, fixed_step: float, max_substeps: int = 12, max_frame_time: float = 0.25):
        self.fixed_step = positive_number(fixed_step, "fixed_step")
        self.max_substeps = integer_at_least(max_substeps, "max_substeps", 1)
        self.max_frame_time = positive_number(max_frame_time, "max_frame_time")
        self.accumulator = 0.0

    def reset(self) -> None:
        self.accumulator = 0.0

    def consume(self, elapsed: float) -> tuple[int, float]:
        elapsed = clamp(finite_number(elapsed, "elapsed"), 0.0, self.max_frame_time)
        self.accumulator += elapsed
        count = min(int(self.accumulator / self.fixed_step), self.max_substeps)
        self.accumulator -= count * self.fixed_step
        # Avoid an unbounded backlog after a paused or suspended application.
        if count == self.max_substeps and self.accumulator >= self.fixed_step:
            self.accumulator %= self.fixed_step
        return count, self.accumulator / self.fixed_step
