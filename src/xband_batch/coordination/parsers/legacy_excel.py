"""Excel list parsers (`legacy_excel` and `user_excel`) driven by coordination_rules.yaml."""

from __future__ import annotations

from pathlib import Path
from typing import List

import openpyxl

from ...config.schemas import CoordinationRulesConfig, ExcelParserRule
from ...errors import ConfigError
from ..models import RawListEntry
from .base import ListParser, register_parser


def _cell_str(value: object) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _parse_excel_with_rule(path: Path, rule: ExcelParserRule) -> List[RawListEntry]:
    if not path.is_file():
        raise ConfigError(path, "file", "coordination input Excel not found")
    wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
    try:
        if rule.sheet not in wb.sheetnames:
            raise ConfigError(path, "sheet", f"sheet {rule.sheet!r} not found in {wb.sheetnames}")
        ws = wb[rule.sheet]
        rows = list(ws.iter_rows(values_only=True))
    finally:
        wb.close()

    if len(rows) < rule.header_row:
        raise ConfigError(path, "header_row", f"sheet {rule.sheet!r} has fewer than {rule.header_row} rows")

    header = [_cell_str(v) for v in rows[rule.header_row - 1]]
    col_idx = {}
    for logical_key, expected_col in rule.columns.items():
        if expected_col in header:
            col_idx[logical_key] = header.index(expected_col)

    if "satellite_network" not in col_idx:
        raise ConfigError(
            path,
            "columns.satellite_network",
            f"column {rule.columns.get('satellite_network')!r} not found in header {header}",
        )

    entries: List[RawListEntry] = []
    for offset, raw in enumerate(rows[rule.first_data_row - 1 :]):
        excel_row = rule.first_data_row + offset

        def _get(key: str) -> str:
            idx = col_idx.get(key)
            if idx is None or idx >= len(raw):
                return ""
            return _cell_str(raw[idx])

        net_cell = _get("satellite_network")
        if not net_cell:
            continue
        entries.append(
            RawListEntry(
                source=f"{path.name}!{rule.sheet}:R{excel_row}",
                raw_network_cell=net_cell,
                frequency_band=_get("frequency_band"),
                operator=_get("operator"),
                notice_id=_get("notice_id"),
                status=_get("status"),
                reason=_get("reason"),
                chn_stations_text=_get("chn_stations"),
            )
        )
    return entries


@register_parser("legacy_excel")
class LegacyExcelParser(ListParser):
    def parse(self, path: Path, rules: CoordinationRulesConfig) -> List[RawListEntry]:
        return _parse_excel_with_rule(path, rules.legacy_excel)


@register_parser("user_excel")
class UserExcelParser(ListParser):
    def parse(self, path: Path, rules: CoordinationRulesConfig) -> List[RawListEntry]:
        return _parse_excel_with_rule(path, rules.user_excel)
