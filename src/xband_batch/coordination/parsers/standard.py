"""Standard flat coordination CSV parser."""

from __future__ import annotations

import csv
from pathlib import Path
from typing import List

from ...config.schemas import CoordinationRulesConfig
from ...errors import ConfigError
from ..models import RawListEntry
from .base import ListParser, register_parser


@register_parser("standard")
class StandardTableParser(ListParser):
    def parse(self, path: Path, rules: CoordinationRulesConfig) -> List[RawListEntry]:
        if not path.is_file():
            raise ConfigError(path, "file", "standard coordination CSV not found")
        entries: List[RawListEntry] = []
        with path.open("r", encoding="utf-8-sig", newline="") as fh:
            for idx, row in enumerate(csv.DictReader(fh), start=2):
                name = (row.get("raw_name") or row.get("matched_name") or "").strip()
                if not name:
                    continue
                entries.append(
                    RawListEntry(
                        source=row.get("source") or f"{path.name}:R{idx}",
                        raw_network_cell=name,
                        frequency_band=row.get("overlap_bands", ""),
                        notice_id=row.get("ntc_id", ""),
                        reason=row.get("review_note", ""),
                        chn_stations_text=row.get("listed_chn_stations", ""),
                    )
                )
        return entries
