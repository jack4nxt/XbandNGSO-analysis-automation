"""Pluggable coordination list parser base & registry."""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Dict, List, Type

from ...config.schemas import CoordinationRulesConfig
from ...errors import ConfigError
from ..models import RawListEntry

_PARSERS: Dict[str, Type["ListParser"]] = {}


def register_parser(kind: str):
    """Decorator to register a coordination list parser under `kind`."""

    def _wrap(cls: Type["ListParser"]) -> Type["ListParser"]:
        _PARSERS[kind] = cls
        return cls

    return _wrap


def get_parser(kind: str) -> "ListParser":
    cls = _PARSERS.get(kind)
    if cls is None:
        raise ConfigError("coordination.inputs", "kind", f"unsupported parser kind {kind!r}; "
                          f"registered: {sorted(_PARSERS)}")
    return cls()


class ListParser(ABC):
    @abstractmethod
    def parse(self, path: Path, rules: CoordinationRulesConfig) -> List[RawListEntry]:
        """Read input document and return raw list entries before splitting/normalizing."""
