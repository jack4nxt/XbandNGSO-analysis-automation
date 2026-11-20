"""Provenance records for user parameter overrides."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional


@dataclass
class OverrideRecord:
    override_id: str
    network: str
    role: str
    kind: str
    target_key: str
    field: str
    declared: Any
    effective: Any
    curve_policy: Optional[str]
    reason: str
