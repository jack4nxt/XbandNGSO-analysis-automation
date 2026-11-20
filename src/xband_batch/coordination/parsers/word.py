"""Word (.docx) coordination list table parser."""

from __future__ import annotations

from pathlib import Path
from typing import List

from ...config.schemas import CoordinationRulesConfig
from ...errors import ConfigError
from ..models import RawListEntry
from .base import ListParser, register_parser


def _cell_text(cell) -> str:
    return (cell.text or "").strip()


@register_parser("word")
class WordTableParser(ListParser):
    def parse(self, path: Path, rules: CoordinationRulesConfig) -> List[RawListEntry]:
        if not path.is_file():
            raise ConfigError(path, "file", "Word coordination file not found")
        try:
            import docx
        except ImportError as exc:
            raise ConfigError(
                path,
                "dependency",
                "python-docx is required to parse Word files (pip install python-docx)",
            ) from exc

        doc = docx.Document(str(path))
        if not doc.tables:
            raise ConfigError(path, "tables", "no tables found in Word document")

        rule = rules.word
        t_indices = [rule.table_index] if rule.table_index is not None else list(range(len(doc.tables)))
        entries: List[RawListEntry] = []

        for tidx in t_indices:
            if tidx < 0 or tidx >= len(doc.tables):
                raise ConfigError(path, "word.table_index", f"table index {tidx} out of range (count={len(doc.tables)})")
            tbl = doc.tables[tidx]
            if not tbl.rows:
                continue
            header = [_cell_text(c) for c in tbl.rows[0].cells]
            col_idx = {k: header.index(col) for k, col in rule.columns.items() if col in header}
            if "satellite_network" not in col_idx:
                if rule.table_index is not None:
                    raise ConfigError(
                        path,
                        "word.columns.satellite_network",
                        f"column {rule.columns.get('satellite_network')!r} not found in table {tidx} header {header}",
                    )
                continue
            for r_i, row in enumerate(tbl.rows[1:], start=2):
                cells = row.cells
                def _get(key: str) -> str:
                    i = col_idx.get(key)
                    return _cell_text(cells[i]) if (i is not None and i < len(cells)) else ""

                net_cell = _get("satellite_network")
                if not net_cell:
                    continue
                entries.append(
                    RawListEntry(
                        source=f"{path.name}!T{tidx}:R{r_i}",
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
