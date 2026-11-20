"""Match normalized network names against CatalogAdapter without guessing across ambiguities."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from ..adapters.base import CatalogAdapter, NetworkCandidate
from ..config.schemas import CoordinationDecisionEntry, MatchRule
from .models import NormalizedEntry


@dataclass
class MatchOutcome:
    entry: NormalizedEntry
    status: str  # "matched" | "ambiguous" | "unmatched" | "skipped"
    chosen: Optional[NetworkCandidate] = None
    candidates: List[NetworkCandidate] = None  # type: ignore[assignment]
    note: str = ""

    def __post_init__(self) -> None:
        if self.candidates is None:
            self.candidates = []


def select_best_candidate(
    candidates: List[NetworkCandidate],
    catalog: CatalogAdapter,
    rule: MatchRule,
    bands: List[Dict[str, Any]] | None = None,
    country_filter: str | None = None,
) -> NetworkCandidate:
    """Pick one notice among same-(sat_name, adm) candidates.

    When `rule.prefer_associated_earth_stations` and `bands` are provided, notices carrying
    overlapping target-band associated specific earth stations outrank empty/out-of-band notices
    (breaking ties by latest receipt date and `ntc_id`).
    """
    if not (rule.prefer_associated_earth_stations and bands):
        return sorted(candidates, key=lambda c: (c.d_rcv or "", c.ntc_id), reverse=True)[0]

    def _score(cand: NetworkCandidate) -> tuple[int, int, str, int]:
        has_stations = 0
        has_carriers = 0
        if cand.orbit_type == "NGSO":
            try:
                ext = catalog.describe_network(cand.ntc_id, bands, country_filter=country_filter)
                has_stations = 1 if ext.stations else 0
                has_carriers = 1 if ext.carriers else 0
            except KeyError:
                pass
        return (has_stations, has_carriers, cand.d_rcv or "", cand.ntc_id)

    return sorted(candidates, key=_score, reverse=True)[0]


def match_entries(
    entries: List[NormalizedEntry],
    catalog: CatalogAdapter,
    rule: MatchRule,
    decisions: Dict[str, CoordinationDecisionEntry] | None = None,
    bands: List[Dict[str, Any]] | None = None,
    country_filter: str | None = None,
) -> List[MatchOutcome]:
    decisions = decisions or {}
    outcomes: List[MatchOutcome] = []

    for item in entries:
        # 1. Explicit user decision overrides catalog search ambiguity
        dec = decisions.get(item.raw_name) or decisions.get(item.normalized_name)
        if dec is not None:
            if dec.action == "skip":
                outcomes.append(MatchOutcome(entry=item, status="skipped", note=dec.reason))
                continue
            cand = NetworkCandidate(
                sat_name=dec.matched_name or item.normalized_name,
                ntc_id=dec.ntc_id or 0,
                adm=dec.adm or "",
                orbit_type="NGSO",
            )
            outcomes.append(MatchOutcome(entry=item, status="matched", chosen=cand, candidates=[cand], note=dec.reason))
            continue

        # 2. Try configured matching strategies in order
        found: List[NetworkCandidate] = []
        for strategy in rule.strategies:
            found = catalog.find_networks(item.normalized_name, strategy=strategy)
            if found:
                break

        if not found:
            outcomes.append(MatchOutcome(entry=item, status="unmatched", note="no catalog match"))
            continue

        distinct_notices = {(c.sat_name, c.ntc_id, c.adm) for c in found}
        if len(distinct_notices) == 1:
            outcomes.append(MatchOutcome(entry=item, status="matched", chosen=found[0], candidates=found))
            continue

        # Multiple candidates / multiple notices
        adms = {c.adm for c in found}
        names = {c.sat_name for c in found}
        if rule.allow_multiple_notices and len(adms) == 1 and len(names) == 1:
            chosen = select_best_candidate(
                found,
                catalog=catalog,
                rule=rule,
                bands=bands,
                country_filter=country_filter,
            )
            outcomes.append(
                MatchOutcome(
                    entry=item,
                    status="matched",
                    chosen=chosen,
                    candidates=found,
                    note=f"selected notice {chosen.ntc_id} of {len(found)} per allow_multiple_notices",
                )
            )
            continue

        outcomes.append(
            MatchOutcome(
                entry=item,
                status="ambiguous",
                candidates=found,
                note=f"{len(found)} candidate notices; explicit decision required",
            )
        )

    return outcomes
