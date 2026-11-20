"""YAML loader with ${ENV} expansion, relative-path resolution, and strict schema checks."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Type, TypeVar

import yaml
from pydantic import BaseModel, ValidationError

from ..errors import ConfigError

T = TypeVar("T", bound=BaseModel)

_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def expand_env_str(value: str, source: Path) -> str:
    """Expand ${VAR} placeholders; raise ConfigError if a referenced variable is unset."""

    def _replace(match: re.Match[str]) -> str:
        var = match.group(1)
        if var not in os.environ or not os.environ[var]:
            raise ConfigError(source, var, f"environment variable ${{{var}}} is not set")
        return os.environ[var]

    return _ENV_PATTERN.sub(_replace, value)


def expand_env_tree(obj: Any, source: Path) -> Any:
    if isinstance(obj, str):
        return expand_env_str(obj, source)
    if isinstance(obj, list):
        return [expand_env_tree(item, source) for item in obj]
    if isinstance(obj, dict):
        return {k: expand_env_tree(v, source) for k, v in obj.items()}
    return obj


def resolve_rel_path(path_value: str, base_dir: Path) -> Path:
    p = Path(path_value)
    return p if p.is_absolute() else (base_dir / p).resolve()


def read_yaml_dict(path: Path) -> dict[str, Any]:
    path = Path(path).resolve()
    if not path.is_file():
        raise ConfigError(path, "file", "not found")
    with path.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    if not isinstance(raw, dict):
        raise ConfigError(path, "root", "YAML root must be a mapping")
    return expand_env_tree(raw, path)


def load_model(model_cls: Type[T], path: Path) -> T:
    path = Path(path).resolve()
    data = read_yaml_dict(path)
    try:
        return model_cls.model_validate(data)
    except ValidationError as exc:
        msgs = [f"{'.'.join(str(x) for x in e['loc'])}: {e['msg']}" for e in exc.errors()]
        raise ConfigError(path, "schema", "; ".join(msgs)) from None
