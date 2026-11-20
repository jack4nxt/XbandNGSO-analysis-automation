"""Matching logic for persistent user antenna decisions."""

from __future__ import annotations

import fnmatch
from typing import List, Optional

from ..config.schemas import AntennaDecisionEntry
from .models import AntennaRequest


def _match_field(pattern: str, value: str) -> bool:
    if pattern == "*":
        return True
    return fnmatch.fnmatchcase(value.upper(), pattern.upper())


def find_matching_decisions(
    decisions: List[AntennaDecisionEntry],
    req: AntennaRequest,
    issue_code: Optional[str],
) -> List[AntennaDecisionEntry]:
    matched: List[AntennaDecisionEntry] = []
    for dec in decisions:
        k = dec.key
        if not _match_field(k.network, req.network):
            continue
        if not _match_field(k.beam, req.beam):
            continue
        if k.emi_rcp != "*" and k.emi_rcp != req.emi_rcp:
            continue
        if k.role != "*" and k.role != req.role:
            continue
        if not _match_field(k.variant, req.variant):
            continue
        if issue_code is not None and dec.issue != "*" and dec.issue != issue_code:
            continue
        matched.append(dec)
    return matched
