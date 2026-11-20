"""Batch preflight gatekeeper: checks every participating beam & earth station without guessing."""

from __future__ import annotations

import csv
import json
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..adapters.base import CarrierDescriptor, EarthStationDescriptor, EngineAdapter, NetworkExtract
from ..antenna.models import AntennaKey, AntennaRequest, PendingAntennaItem, ResolvedAntenna
from ..antenna.resolver import AntennaResolver
from ..config.loader import load_model, resolve_rel_path
from ..config.schemas import (
    AntennaDecisionsConfig,
    AntennaPolicyConfig,
    BatchConfig,
    OverridesConfig,
)
from ..coordination import CoordinationRow, load_coordination_table
from ..errors import BatchError
from ..overrides import apply_overrides
from ..paths import Layout
from .models import PairPlan, PreflightReport


# ----------------------------------------------------------------------------- planning
def load_evaluated_context(layout: Layout, batch_id: str) -> Dict[str, Any]:
    path = layout.coordination_context_path(batch_id)
    if path.is_file():
        return json.loads(path.read_text(encoding="utf-8")).get("evaluated", {})
    return {}


def build_pair_plans(
    batch_cfg: BatchConfig,
    rows: List[CoordinationRow],
    evaluated_ntc_id: int = 0,
) -> List[PairPlan]:
    ready = [r for r in rows if r.category == "READY_CO_DIR" and r.ntc_id is not None]
    plans: List[PairPlan] = []
    idx = 0
    for r in ready:
        target = r.matched_name or r.normalized_name
        if batch_cfg.evaluated_role in ("interferer", "both"):
            idx += 1
            plans.append(
                PairPlan(idx, batch_cfg.evaluated_network, evaluated_ntc_id, target, r.ntc_id, r.overlap_bands)  # type: ignore[arg-type]
            )
        if batch_cfg.evaluated_role in ("victim", "both"):
            idx += 1
            plans.append(
                PairPlan(idx, target, r.ntc_id, batch_cfg.evaluated_network, evaluated_ntc_id, r.overlap_bands)  # type: ignore[arg-type]
            )
    return plans


# ----------------------------------------------------------------------------- participation
def _overlap(a: CarrierDescriptor, b: CarrierDescriptor) -> bool:
    return min(a.f_high_mhz, b.f_high_mhz) > max(a.f_low_mhz, b.f_low_mhz)


def participating_carriers(
    a: NetworkExtract, b: NetworkExtract
) -> Tuple[List[CarrierDescriptor], List[CarrierDescriptor]]:
    """Carriers of A and B that form at least one scenario (same direction, overlap, stations on both)."""
    used_a: List[CarrierDescriptor] = []
    used_b: List[CarrierDescriptor] = []
    for ca in a.carriers:
        for cb in b.carriers:
            if ca.direction != cb.direction or not _overlap(ca, cb):
                continue
            if not ca.station_ids or not cb.station_ids:
                continue
            if ca not in used_a:
                used_a.append(ca)
            if cb not in used_b:
                used_b.append(cb)
    return used_a, used_b


def earth_variant(st: EarthStationDescriptor) -> str:
    params = st.extra.get("model_params", {})
    extra = ",".join(f"{k}={params[k]:.4g}" if isinstance(params[k], float) else f"{k}={params[k]}"
                     for k in sorted(params))
    gain = "-" if st.gmax_dbi is None else f"{st.gmax_dbi:.2f}"
    diam = "-" if st.ant_diam_m is None else f"{st.ant_diam_m:.3g}"
    return f"{st.ant_type or '-'}|{gain}|{diam}|{extra}"


def _space_request(net: NetworkExtract, c: CarrierDescriptor, altitude_km: Optional[float]) -> AntennaRequest:
    params: Dict[str, Any] = dict(c.extra.get("model_params", {}))
    if c.sat_beamlet_deg is not None:
        params["beamlet"] = c.sat_beamlet_deg
    return AntennaRequest(
        network=net.sat_name,
        ntc_id=net.ntc_id,
        beam=c.beam_name,
        emi_rcp=c.emi_rcp,  # type: ignore[arg-type]
        role="space",
        declared_gmax_dbi=c.sat_gain_dbi,
        model_name=c.sat_pattern_id,
        model_params=params,
        altitude_km=altitude_km,
        raw_image_path=c.extra.get("raw_image_path"),
    )


def _earth_request(net: NetworkExtract, c: CarrierDescriptor, st: EarthStationDescriptor) -> AntennaRequest:
    params: Dict[str, Any] = dict(st.extra.get("model_params", {}))
    if st.beamwidth_deg is not None:
        params["beamwidth_deg"] = st.beamwidth_deg
    if st.ant_diam_m is not None:
        params["ant_diam_m"] = st.ant_diam_m
    return AntennaRequest(
        network=net.sat_name,
        ntc_id=net.ntc_id,
        beam=c.beam_name,
        emi_rcp=c.emi_rcp,  # type: ignore[arg-type]
        role="earth",
        declared_gmax_dbi=st.gmax_dbi,
        declared_beamwidth_deg=st.beamwidth_deg,
        model_name=st.ant_type,
        model_params=params,
        variant=earth_variant(st),
        raw_image_path=st.extra.get("raw_image_path"),
    )


def _min_altitude(net: NetworkExtract) -> Optional[float]:
    alts = [p.altitude_km for p in net.planes if p.altitude_km is not None]
    return min(alts) if alts else None


def pair_requests(
    decl_a: NetworkExtract,
    eff_a: NetworkExtract,
    decl_b: NetworkExtract,
    eff_b: NetworkExtract,
) -> List[AntennaRequest]:
    """Antenna requests of one pair.

    Space beams: the *declared* carrier (curve_policy of an override adjusts the curve later).
    Earth stations: the *effective* station (a model is re-evaluated at the effective gain).
    Participation is judged on the effective (overridden) networks.
    """
    used_a, used_b = participating_carriers(eff_a, eff_b)
    reqs: List[AntennaRequest] = []
    seen = set()
    for decl, eff, used in ((decl_a, eff_a, used_a), (decl_b, eff_b, used_b)):
        decl_by_id = {c.carrier_id: c for c in decl.carriers}
        alt = _min_altitude(decl)
        for c in used:
            req = _space_request(decl, decl_by_id.get(c.carrier_id, c), alt)
            if req.key not in seen:
                seen.add(req.key)
                reqs.append(req)
            for st in eff.stations:
                if st.beam_name != c.beam_name or st.emi_rcp != c.emi_rcp or st.station_id not in c.station_ids:
                    continue
                ereq = _earth_request(eff, c, st)
                if ereq.key not in seen:
                    seen.add(ereq.key)
                    reqs.append(ereq)
    return reqs


# ----------------------------------------------------------------------------- reports
def _write_outputs(
    out_dir: Path,
    csv_name: str,
    md_name: str,
    batch_id: str,
    pending: List[PendingAntennaItem],
    resolved: Dict[AntennaKey, ResolvedAntenna],
    unresolved_coord: int,
    empty_plans: List[PairPlan],
    extraction_errors: Dict[str, str],
) -> Tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / csv_name
    md_path = out_dir / md_name

    with csv_path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(
            fh,
            fieldnames=["network", "ntc_id", "beam", "emi_rcp", "role", "variant", "declared_gmax_dbi",
                        "altitude_km", "attempted_sources", "issue_codes", "issue_details", "raw_image_path"],
        )
        writer.writeheader()
        for item in pending:
            writer.writerow({
                "network": item.network, "ntc_id": item.ntc_id, "beam": item.beam, "emi_rcp": item.emi_rcp,
                "role": item.role, "variant": item.variant,
                "declared_gmax_dbi": "" if item.declared_gmax_dbi is None else item.declared_gmax_dbi,
                "altitude_km": "" if item.altitude_km is None else item.altitude_km,
                "attempted_sources": ";".join(item.attempted_sources),
                "issue_codes": ";".join(i.code for i in item.issues),
                "issue_details": " | ".join(i.detail for i in item.issues),
                "raw_image_path": item.raw_image_path or "",
            })

    by_source = Counter(r.source_used or r.status for r in resolved.values() if r.status != "pending")
    lines = [
        f"# Preflight report: {batch_id}",
        "",
        f"- Unresolved coordination rows (`NEEDS_REVIEW`/`UNMATCHED`): **{unresolved_coord}**",
        f"- Antenna requests: **{len(resolved)}** (resolved/excluded {len(resolved) - len(pending)}, pending **{len(pending)}**)",
        f"- Pairs without any scenario: **{len(empty_plans)}**",
        f"- Network extraction errors: **{len(extraction_errors)}**",
        "",
        "## Resolved by source",
        "",
        "| Source | Count |",
        "|---|---|",
    ]
    for src, n in sorted(by_source.items()):
        lines.append(f"| `{src}` | {n} |")
    if extraction_errors:
        lines += ["", "## Network extraction errors (ITU data inconsistencies)", ""]
        for k, msg in sorted(extraction_errors.items()):
            lines.append(f"- `{k}`: {msg}")
    if empty_plans:
        lines += ["", "## Pairs with no scenario (no overlapping carriers with stations)", ""]
        lines += [f"- #{p.index}: {p.interferer} vs {p.victim}" for p in empty_plans]
    if pending:
        lines += ["", "## Pending antenna items (decision required, nothing was estimated)", "",
                  "| Network | Beam | E/R | Role | Variant | Gmax (dBi) | Issues | Detail |",
                  "|---|---|---|---|---|---|---|---|"]
        for item in pending:
            codes = ", ".join(i.code for i in item.issues)
            details = "; ".join(i.detail for i in item.issues).replace("|", "/")
            gmax = "-" if item.declared_gmax_dbi is None else f"{item.declared_gmax_dbi:g}"
            lines.append(f"| `{item.network}` | `{item.beam}` | `{item.emi_rcp}` | `{item.role}` | "
                         f"`{item.variant}` | {gmax} | `{codes}` | {details} |")
    else:
        lines += ["", "All participating space beams and earth stations passed the antenna gate."]
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return csv_path, md_path


# ----------------------------------------------------------------------------- main
def run_preflight(
    batch_cfg: BatchConfig,
    batch_dir_base: Path,
    layout: Layout,
    engine: EngineAdapter,
    source_context: Dict[str, Any] | None = None,
) -> PreflightReport:
    rows = load_coordination_table(layout.coordination_table_path(batch_cfg.batch_id))
    unresolved_coord = sum(1 for r in rows if r.category in ("NEEDS_REVIEW", "UNMATCHED"))
    evaluated = load_evaluated_context(layout, batch_cfg.batch_id)
    plans = build_pair_plans(batch_cfg, rows, int(evaluated.get("ntc_id") or 0))

    policy = load_model(AntennaPolicyConfig, resolve_rel_path(batch_cfg.antenna.policy, batch_dir_base))
    decisions_cfg = load_model(AntennaDecisionsConfig, resolve_rel_path(batch_cfg.antenna.decisions, batch_dir_base))
    overrides_cfg = (
        load_model(OverridesConfig, resolve_rel_path(batch_cfg.overrides, batch_dir_base))
        if batch_cfg.overrides else None
    )

    ctx = source_context
    if ctx is None:
        provider = getattr(engine, "antenna_source_context", None)
        ctx = provider(policy, batch_cfg.engine.options) if provider else {}
    resolver = AntennaResolver(policy, decisions=decisions_cfg.decisions, source_context=ctx)

    extracted: Dict[Tuple[str, int], NetworkExtract] = {}
    effective: Dict[Tuple[str, int, str], NetworkExtract] = {}
    extraction_errors: Dict[str, str] = {}

    def _declared(name: str, ntc: int) -> Optional[NetworkExtract]:
        if (name, ntc) in extracted:
            return extracted[(name, ntc)]
        if f"{name}#{ntc}" in extraction_errors:
            return None
        try:
            extracted[(name, ntc)] = engine.extract_network(name, ntc or None, batch_cfg.engine.options)
            return extracted[(name, ntc)]
        except BatchError as exc:
            extraction_errors[f"{name}#{ntc}"] = str(exc)
            return None

    def _effective(name: str, ntc: int, role: str) -> Optional[NetworkExtract]:
        if (name, ntc, role) not in effective:
            decl = _declared(name, ntc)
            if decl is None:
                return None
            net, _, _ = apply_overrides(decl, role, overrides_cfg, curves=None)  # type: ignore[arg-type]
            effective[(name, ntc, role)] = net
        return effective[(name, ntc, role)]

    resolved: Dict[AntennaKey, ResolvedAntenna] = {}
    pending: List[PendingAntennaItem] = []
    pair_keys: Dict[int, List[AntennaKey]] = {}
    empty: List[PairPlan] = []

    for plan in plans:
        da, db = _declared(plan.interferer, plan.interferer_ntc_id), _declared(plan.victim, plan.victim_ntc_id)
        ea = _effective(plan.interferer, plan.interferer_ntc_id, "interferer")
        eb = _effective(plan.victim, plan.victim_ntc_id, "victim")
        if da is None or db is None or ea is None or eb is None:
            continue
        reqs = pair_requests(da, ea, db, eb)
        if not reqs:
            empty.append(plan)
            continue
        pair_keys[plan.index] = [r.key for r in reqs]
        for req in reqs:
            if req.key in resolved:
                continue
            res = resolver.resolve(req)
            resolved[req.key] = res
            if res.status == "pending" and res.pending_item is not None:
                pending.append(res.pending_item)

    csv_path, md_path = _write_outputs(
        layout.preflight_dir(batch_cfg.batch_id) / layout.timestamp(),
        layout.cfg.layout.pending_antennas_csv,
        layout.cfg.layout.pending_antennas_md,
        batch_cfg.batch_id, pending, resolved, unresolved_coord, empty, extraction_errors,
    )
    return PreflightReport(
        batch_id=batch_cfg.batch_id,
        passed=(not pending) and unresolved_coord == 0 and not empty and not extraction_errors,
        pair_plans=plans,
        resolved_antennas=resolved,
        pending_items=pending,
        unresolved_coordination_count=unresolved_coord,
        pending_csv_path=csv_path,
        pending_md_path=md_path,
        extracted=extracted,
        pair_keys=pair_keys,
        empty_plans=[p.index for p in empty],
        extraction_errors=extraction_errors,
    )
