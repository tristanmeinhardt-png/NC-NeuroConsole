"""Lazy registry for native NC standard-library modules."""

from __future__ import annotations

import importlib
from typing import Any


_FACTORIES: dict[str, tuple[str, str]] = {
    "physics2d": ("nc_physics2d", "create_nc_module"),
    "physics3d": ("nc_physics3d", "create_nc_module"),
}


def builtin_names() -> tuple[str, ...]:
    return tuple(sorted(_FACTORIES))


def build_builtin_module(name: str, interpreter: Any, module_class: type) -> Any | None:
    target = _FACTORIES.get(str(name))
    if target is None:
        return None
    module_name, factory_name = target
    python_module = importlib.import_module(module_name)
    factory = getattr(python_module, factory_name)
    exports = factory(interpreter)
    if not isinstance(exports, dict):
        raise RuntimeError(f"Native NC module factory '{module_name}.{factory_name}' returned invalid exports")
    module = module_class(str(name))
    module.namespace = dict(exports)
    module.exports = dict(exports)
    return module
