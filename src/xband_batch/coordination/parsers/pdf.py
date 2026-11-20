"""PDF coordination list table parser."""

from __future__ import annotations

from pathlib import Path
from typing import List

from ...config.schemas import CoordinationRulesConfig
from ...errors import ConfigError
from ..models import RawListEntry
from .base import ListParser, register_parser


def _cell_text(val: object) -> str:
    return "" if val is None else str(val).strip()


@register_parser("pdf")
class PdfTableParser(ListParser):
    def parse(self, path: Path, rules: CoordinationRulesConfig) -> List[RawListEntry]:
        if not path.is_file():
            raise ConfigError(path, "file", "PDF coordination file not found")
        try:
            import pdfplumber
        except ImportError as exc:
            raise ConfigError(
                path,
                "dependency",
                "pdfplumber is required to parse PDF files (pip install pdfplumber)",
            ) from exc

        rule = rules.pdf
        entries: List[RawListEntry] = []
        col_idx = None

        with pdfplumber.open(str(path)) as pdf:
            for pno, page in enumerate(pdf.pages, start=1):
                for t_i, table in enumerate(page.extract_tables() or []):
                    if not table:
                        continue
                    header_cand = [_cell_text(c) for c in table[0]]
                    cand_map = {k: header_cand.index(col) for k, col in rule.columns.items() if col in header_cand}
                    if "satellite_network" in cand_map:
                        col_idx = cand_map
                        data_rows = table[1:]
                    elif col_idx is not None:
                        # Continuation table across page break sharing previous header
                        data_rows = table
                    else:
                        continue

                    for r_i, raw in enumerate(data_rows, start=2):
                        def _get(key: str) -> str:
                            i = col_idx.get(key)  # type: ignore[union-attr]
                            return _cell_text(raw[i]) if (i is not None and i < len(raw)) else ""

                        net_cell = _get("satellite_network")
                        if not net_cell:
                            continue
                        entries.append(
                            RawListEntry(
                                source=f"{path.name}!P{pno}T{t_i}:R{r_i}",
                                raw_network_cell=net_cell,
                                frequency_band=_get("frequency_band"),
                                operator=_get("operator"),
                                notice_id=_get("notice_id"),
                                status=_get("status"),
                                reason=_get("reason"),
                                chn_stations_text=_get("chn_stations"),
                            )
                        )

        if col_idx is None:
            raise ConfigError(
                path,
                "pdf.columns.satellite_network",
                f"no table containing header {rule.columns.get('satellite_network')!r} found in PDF",
            )
        return entries
