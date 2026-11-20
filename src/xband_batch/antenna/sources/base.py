"""Pluggable antenna source base & registry."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict, Type

from ...config.schemas import AntennaPolicyConfig
from ...errors import ConfigError
from ..models import AntennaRequest, SourceFetchResult

_SOURCES: Dict[str, Type["AntennaSource"]] = {}


def register_source(name: str):
    """Decorator registering an AntennaSource subclass under `name`."""

    def _wrap(cls: Type["AntennaSource"]) -> Type["AntennaSource"]:
        _SOURCES[name] = cls
        return cls

    return _wrap


def create_source(name: str, context: Dict[str, Any] | None = None) -> "AntennaSource":
    cls = _SOURCES.get(name)
    if cls is None:
        raise ConfigError(
            "antenna_policy",
            "source_order",
            f"unknown antenna source {name!r}; registered: {sorted(_SOURCES)}",
        )
    return cls(context or {})


class AntennaSource(ABC):
    def __init__(self, context: Dict[str, Any] | None = None):
        self.context = context or {}

    @abstractmethod
    def fetch(self, req: AntennaRequest, policy: AntennaPolicyConfig) -> SourceFetchResult:
        """Attempt to obtain a candidate curve without silent estimation or fallback."""
