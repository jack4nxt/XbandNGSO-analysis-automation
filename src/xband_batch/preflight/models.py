"""Models for batch preflight (pair planning + antenna gatekeeper)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Tuple

from ..adapters.base import NetworkExtract
from ..antenna.models import AntennaKey, PendingAntennaItem, ResolvedAntenna


@dataclass
class PairPlan:
    index: int
    interferer: str
    interferer_ntc_id: int
    victim: str
    victim_ntc_id: int
    overlap_bands: str


@dataclass
class PreflightReport:
    batch_id: str
    passed: bool
    pair_plans: List[PairPlan]
    resolved_antennas: Dict[AntennaKey, ResolvedAntenna] = field(default_factory=dict)
    pending_items: List[PendingAntennaItem] = field(default_factory=list)
    unresolved_coordination_count: int = 0
    pending_csv_path: Path | None = None
    pending_md_path: Path | None = None
    # declared (un-overridden) networks as extracted, keyed (name, ntc_id); reused by freeze
    extracted: Dict[Tuple[str, int], NetworkExtract] = field(default_factory=dict)
    # per plan index -> antenna keys that take part in that pair
    pair_keys: Dict[int, List[AntennaKey]] = field(default_factory=dict)
    # plans that cannot produce any scenario (no overlapping carriers with stations)
    empty_plans: List[int] = field(default_factory=list)
    # networks whose ITU extraction raised an error (never hidden)
    extraction_errors: Dict[str, str] = field(default_factory=dict)
