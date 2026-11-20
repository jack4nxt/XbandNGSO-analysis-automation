"""Data models for antenna resolution, validation checks, and preflight pending items."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional, Tuple

IssueCode = Literal[
    "PHI_RANGE_SHORT",
    "PEAK_MISMATCH",
    "BEAMWIDTH_MISMATCH",
    "NON_FINITE",
    "NOT_MONOTONIC",
    "MISSING_PARAMS",
    "UNMAPPED_MODEL",
    "AMBIGUOUS_SOURCE",
    "NO_SOURCE",
]

# (network, beam, emi_rcp, role, variant); variant is "" for space beams
AntennaKey = Tuple[str, str, str, str, str]


class AmbiguousPatternError(Exception):
    """A source holds several different patterns for the same key (never guess which one)."""


class DecisionNotApplicable(Exception):
    """An approved decision exists but its own limits (e.g. max_gap_deg) are not met."""


@dataclass
class AntennaRequest:
    network: str
    ntc_id: int
    beam: str
    emi_rcp: Literal["E", "R"]
    role: Literal["space", "earth"]
    declared_gmax_dbi: Optional[float]
    declared_beamwidth_deg: Optional[float] = None
    model_name: Optional[str] = None
    model_params: Dict[str, Any] = field(default_factory=dict)
    altitude_km: Optional[float] = None
    raw_image_path: Optional[str] = None
    variant: str = ""

    @property
    def key(self) -> AntennaKey:
        return (self.network, self.beam, self.emi_rcp, self.role, self.variant)


@dataclass
class AntennaCurve:
    phi_deg: List[float]
    gain_dbi: List[float]
    source_name: str
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class AntennaIssue:
    code: IssueCode
    detail: str


@dataclass
class SourceFetchResult:
    """Outcome from one AntennaSource.fetch()."""

    status: Literal["found", "absent", "error"]
    curve: Optional[AntennaCurve] = None
    issues: List[AntennaIssue] = field(default_factory=list)
    note: str = ""


@dataclass
class PendingAntennaItem:
    network: str
    ntc_id: int
    beam: str
    emi_rcp: str
    role: str
    declared_gmax_dbi: Optional[float]
    altitude_km: Optional[float]
    attempted_sources: List[str]
    issues: List[AntennaIssue]
    raw_image_path: Optional[str] = None
    variant: str = ""


@dataclass
class ResolvedAntenna:
    request: AntennaRequest
    status: Literal["resolved", "pending", "excluded"]
    curve: Optional[AntennaCurve] = None
    source_used: str = ""
    issues_detected: List[AntennaIssue] = field(default_factory=list)
    applied_decisions: List[Tuple[str, str]] = field(default_factory=list)  # [(decision_id, action)]
    pending_item: Optional[PendingAntennaItem] = None
