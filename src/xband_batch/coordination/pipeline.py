"""End-to-end coordination list parsing, matching, classification, and reporting."""

from __future__ import annotations

import csv
import json
from collections import Counter
from dataclasses import asdict
from pathlib import Path
from typing import Dict, List

from ..adapters.base import CatalogAdapter
from ..config.loader import load_model, resolve_rel_path
from ..config.schemas import (
    BatchConfig,
    CoordinationDecisionsConfig,
    CoordinationRulesConfig,
)
from ..errors import ConfigError
from ..paths import Layout
from .classifier import band_text_matches, classify_outcome
from .matcher import match_entries, select_best_candidate
from .models import CoordinationRow, RawListEntry, ValidationReport
from .normalizer import normalize_entries
from .parsers import get_parser


def load_coordination_table(path: Path) -> List[CoordinationRow]:
    if not path.is_file():
        raise ConfigError(path, "file", "coordination_table.csv not found; run `parse-list` first")
    rows: List[CoordinationRow] = []
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        for r in csv.DictReader(fh):
            rows.append(
                CoordinationRow(
                    evaluated_network=r["evaluated_network"],
                    raw_name=r["raw_name"],
                    normalized_name=r["normalized_name"],
                    source=r["source"],
                    matched_name=r["matched_name"],
                    ntc_id=int(r["ntc_id"]) if r["ntc_id"] else None,
                    adm=r["adm"],
                    orbit_type=r["orbit_type"],
                    overlap_bands=r["overlap_bands"],
                    category=r["category"],  # type: ignore[arg-type]
                    n_chn_stations=int(r["n_chn_stations"]) if r["n_chn_stations"] else 0,
                    review_note=r["review_note"],
                    candidate_notices=r["candidate_notices"],
                    listed_chn_stations=r["listed_chn_stations"],
                )
            )
    return rows


def write_coordination_table(path: Path, rows: List[CoordinationRow]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "evaluated_network",
        "raw_name",
        "normalized_name",
        "source",
        "matched_name",
        "ntc_id",
        "adm",
        "orbit_type",
        "overlap_bands",
        "category",
        "n_chn_stations",
        "review_note",
        "candidate_notices",
        "listed_chn_stations",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            d = asdict(row)
            d["ntc_id"] = "" if row.ntc_id is None else str(row.ntc_id)
            writer.writerow(d)


def write_validation_report(path: Path, report: ValidationReport) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        f"# Coordination List Validation Report: {report.batch_id}",
        "",
        f"- Evaluated network: `{report.evaluated_network}`",
        f"- Total expanded entries: **{report.total_rows}**",
        "",
        "## Category Breakdown",
        "",
        "| Category | Count |",
        "|---|---|",
    ]
    for cat, cnt in sorted(report.counts_by_category.items()):
        lines.append(f"| `{cat}` | {cnt} |")

    review_rows = [r for r in report.rows if r.category in ("NEEDS_REVIEW", "UNMATCHED")]
    if review_rows:
        lines.extend(["", "## Items Requiring Review / Unmatched", ""])
        for r in review_rows:
            cands = f" | candidates: {r.candidate_notices}" if r.candidate_notices else ""
            lines.append(
                f"- **{r.raw_name}** (`{r.category}`, source `{r.source}`): {r.review_note}{cands}"
            )

    if report.station_discrepancies:
        lines.extend(["", "## Earth-Station Discrepancies vs Listed Column", ""])
        for item in report.station_discrepancies:
            lines.append(f"- {item}")

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def run_parse_list(
    batch_cfg: BatchConfig,
    batch_dir_base: Path,
    layout: Layout,
    catalog: CatalogAdapter,
) -> ValidationReport:
    rules_path = resolve_rel_path(batch_cfg.coordination.rules, batch_dir_base)
    rules = load_model(CoordinationRulesConfig, rules_path)

    decisions: Dict[str, object] = {}
    if batch_cfg.coordination.decisions:
        dec_path = resolve_rel_path(batch_cfg.coordination.decisions, batch_dir_base)
        dec_cfg = load_model(CoordinationDecisionsConfig, dec_path)
        decisions = {d.raw_name: d for d in dec_cfg.decisions}

    raw_entries: List[RawListEntry] = []
    for inp in batch_cfg.coordination.inputs:
        inp_path = resolve_rel_path(inp.path, batch_dir_base)
        parser = get_parser(inp.kind)
        raw_entries.extend(parser.parse(inp_path, rules))

    norm_entries = normalize_entries(raw_entries, rules.normalize)

    band_dicts = [b.model_dump() for b in batch_cfg.bands]
    country = batch_cfg.engine.options.get("country_filter")

    # Resolve evaluated network first (honor coordination.decisions if present)
    eval_dec = decisions.get(batch_cfg.evaluated_network)
    if eval_dec is not None and eval_dec.action == "select" and eval_dec.ntc_id:  # type: ignore[attr-defined]
        eval_ntc_id = int(eval_dec.ntc_id)  # type: ignore[attr-defined]
        eval_adm = eval_dec.adm or ""  # type: ignore[attr-defined]
        eval_sat = eval_dec.matched_name or batch_cfg.evaluated_network  # type: ignore[attr-defined]
    else:
        eval_cands = catalog.find_networks(batch_cfg.evaluated_network, strategy=rules.match.strategies[0])
        if not eval_cands:
            raise ConfigError("batch", "evaluated_network", f"network {batch_cfg.evaluated_network!r} not found")
        if len({(c.sat_name, c.ntc_id, c.adm) for c in eval_cands}) > 1:
            if not rules.match.allow_multiple_notices or len({c.adm for c in eval_cands}) > 1:
                cands_txt = "; ".join(f"{c.sat_name}(ntc={c.ntc_id},adm={c.adm})" for c in eval_cands)
                raise ConfigError(
                    "batch",
                    "evaluated_network",
                    f"multiple candidate notices for {batch_cfg.evaluated_network!r}: {cands_txt}; "
                    "pin one in coordination_decisions.yaml",
                )
            chosen_eval = select_best_candidate(
                eval_cands,
                catalog=catalog,
                rule=rules.match,
                bands=band_dicts,
                country_filter=country,
            )
        else:
            chosen_eval = eval_cands[0]
        eval_ntc_id, eval_adm, eval_sat = chosen_eval.ntc_id, chosen_eval.adm, chosen_eval.sat_name

    eval_extract = catalog.describe_network(eval_ntc_id, band_dicts, country_filter=country)

    # Skip catalog lookups for rows already filtered out by reason or band text
    kept_for_match = []
    pre_filtered_rows: List[CoordinationRow] = []
    for entry in norm_entries:
        raw = entry.raw_entry
        if raw.reason and raw.reason in rules.filter.exclude_reasons:
            pre_filtered_rows.append(
                CoordinationRow(
                    evaluated_network=eval_sat,
                    raw_name=entry.raw_name,
                    normalized_name=entry.normalized_name,
                    source=raw.source,
                    matched_name="",
                    ntc_id=None,
                    adm="",
                    orbit_type="UNKNOWN",
                    overlap_bands="",
                    category="SKIP_FILTERED",
                    n_chn_stations=0,
                    review_note=f"filtered by reason: {raw.reason}",
                    listed_chn_stations=raw.chn_stations_text,
                )
            )
            continue
        if rules.filter.band_text_prefilter and not band_text_matches(
            raw.frequency_band, batch_cfg.bands, rules.filter.band_range_regex
        ):
            pre_filtered_rows.append(
                CoordinationRow(
                    evaluated_network=eval_sat,
                    raw_name=entry.raw_name,
                    normalized_name=entry.normalized_name,
                    source=raw.source,
                    matched_name="",
                    ntc_id=None,
                    adm="",
                    orbit_type="UNKNOWN",
                    overlap_bands="",
                    category="SKIP_FILTERED",
                    n_chn_stations=0,
                    review_note=f"band text {raw.frequency_band!r} outside configured bands",
                    listed_chn_stations=raw.chn_stations_text,
                )
            )
            continue
        kept_for_match.append(entry)

    outcomes = match_entries(
        kept_for_match,
        catalog,
        rules.match,
        decisions=decisions,  # type: ignore[arg-type]
        bands=band_dicts,
        country_filter=country,
    )

    seen_targets = set()
    rows: List[CoordinationRow] = []
    discrepancies: List[str] = []
    for row in pre_filtered_rows:
        dedupe_key = (row.normalized_name, row.ntc_id, row.category)
        if dedupe_key not in seen_targets:
            seen_targets.add(dedupe_key)
            rows.append(row)
    for out in outcomes:
        row = classify_outcome(eval_extract, out, catalog, batch_cfg.bands, rules.filter, country)
        dedupe_key = (row.matched_name or row.normalized_name, row.ntc_id, row.category)
        if dedupe_key in seen_targets:
            continue
        seen_targets.add(dedupe_key)
        rows.append(row)
        if row.listed_chn_stations and row.n_chn_stations == 0 and row.category == "SKIP_NO_CHN_ES":
            discrepancies.append(
                f"{row.raw_name}: list mentions CHN stations ({row.listed_chn_stations!r}) "
                "but catalog returned 0 CHN stations"
            )

    counts = dict(Counter(r.category for r in rows))
    report = ValidationReport(
        batch_id=batch_cfg.batch_id,
        evaluated_network=eval_sat,
        total_rows=len(rows),
        counts_by_category=counts,
        rows=rows,
        station_discrepancies=discrepancies,
    )

    ctx_path = layout.coordination_context_path(batch_cfg.batch_id)
    ctx_path.parent.mkdir(parents=True, exist_ok=True)
    ctx_path.write_text(
        json.dumps(
            {
                "batch_id": batch_cfg.batch_id,
                "evaluated": {
                    "sat_name": eval_sat,
                    "ntc_id": eval_ntc_id,
                    "adm": eval_adm,
                },
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    write_coordination_table(layout.coordination_table_path(batch_cfg.batch_id), rows)
    write_validation_report(layout.validation_report_path(batch_cfg.batch_id), report)
    return report
