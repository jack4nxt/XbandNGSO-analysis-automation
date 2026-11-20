"""Cell splitting and suffix normalization driven exclusively by coordination_rules.yaml."""

from __future__ import annotations

import re
from typing import Iterable, List

from ..config.schemas import NormalizeRule
from .models import NormalizedEntry, RawListEntry


def _split_cell(text: str, separators: List[str]) -> List[str]:
    if not separators:
        return [text.strip()] if text.strip() else []
    pattern = "|".join(re.escape(sep) for sep in separators)
    return [part.strip() for part in re.split(pattern, text) if part.strip()]


def normalize_name(raw_name: str, rule: NormalizeRule) -> str:
    cleaned = raw_name.strip()
    for pat in rule.strip_suffix_patterns:
        cleaned = re.sub(pat, "", cleaned).strip()
    return cleaned.upper() if rule.case_insensitive else cleaned


def normalize_entries(entries: Iterable[RawListEntry], rule: NormalizeRule) -> List[NormalizedEntry]:
    out: List[NormalizedEntry] = []
    for entry in entries:
        for raw_name in _split_cell(entry.raw_network_cell, rule.cell_separators):
            norm = normalize_name(raw_name, rule)
            if norm and any(ch.isalnum() for ch in norm):
                out.append(NormalizedEntry(raw_entry=entry, raw_name=raw_name, normalized_name=norm))
    return out
