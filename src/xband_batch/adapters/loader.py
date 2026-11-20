"""Dynamic adapter loader using 'module.path:ClassName' strings from config."""

from __future__ import annotations

import importlib
from typing import Any, Dict

from ..errors import ConfigError


def load_adapter(spec: str, options: Dict[str, Any] | None = None) -> Any:
    if ":" not in spec:
        raise ConfigError("engine", "adapter", f"expected 'module.path:ClassName', got {spec!r}")
    mod_name, cls_name = spec.split(":", 1)
    try:
        module = importlib.import_module(mod_name)
    except ImportError as exc:
        raise ConfigError("engine", "adapter", f"cannot import module {mod_name!r}: {exc}") from None
    cls = getattr(module, cls_name, None)
    if cls is None:
        raise ConfigError("engine", "adapter", f"class {cls_name!r} not found in {mod_name!r}")
    try:
        return cls(options or {})
    except TypeError:
        return cls()
