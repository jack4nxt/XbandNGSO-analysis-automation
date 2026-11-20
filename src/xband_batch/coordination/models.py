"""Data models for coordination list parsing, matching, and classification."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Literal, Optional

CoordinationCategory = Literal[
    "READY_CO_DIR",
    "DEFERRED_REV_DIR",
    "SKIP_GSO",
    "SKIP_NO_CHN_ES",
    "SKIP_FILTERED",
    "NEEDS_REVIEW",
    "UNMATCHED",
]


@dataclass
class RawListEntry:
    """One raw row/cell extracted from an input coordination document before splitting."""

    source: str
    raw_network_cell: str
    frequency_band: str = ""
    operator: str = ""
    notice_id: str = ""
    status: str = ""
    reason: str = ""
    chn_stations_text: str = ""


@dataclass
class NormalizedEntry:
    """One individual network name split out from a RawListEntry and stripped of suffixes."""

    raw_entry: RawListEntry
    raw_name: str
    normalized_name: str


@dataclass
class CoordinationRow:
    """Standard flat coordination table row: one row = one target satellite network."""

    evaluated_network: str
    raw_name: str
    normalized_name: str
    source: str
    matched_name: str
    ntc_id: Optional[int]
    adm: str
    orbit_type: str  # "NGSO" | "GSO" | "UNKNOWN"
    overlap_bands: str
    category: CoordinationCategory
    n_chn_stations: int
    review_note: str = ""
    candidate_notices: str = ""
    listed_chn_stations: str = ""


@dataclass
class ValidationReport:
    batch_id: str
    evaluated_network: str
    total_rows: int
    counts_by_category: dict[str, int] = field(default_factory=dict)
    rows: List[CoordinationRow] = field(default_factory=list)
    station_discrepancies: List[str] = field(default_factory=list)
