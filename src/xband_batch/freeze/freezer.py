"""Freeze batch inputs after preflight passes: generates immutable per-pair bundles."""

from __future__ import annotations

import json
import shutil
from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict, List

from ..adapters.base import EngineAdapter
from ..antenna.models import AntennaCurve, AntennaKey
from ..config.loader import load_model, resolve_rel_path
from ..config.schemas import BatchConfig, OverridesConfig
from ..errors import PreflightBlockedError
from ..overrides import apply_overrides
from ..paths import Layout
from ..preflight import run_preflight
from .models import FreezeResult, FrozenPair


def _snapshot_configs(
    batch_cfg: BatchConfig,
    batch_yaml_path: Path,
    batch_dir_base: Path,
    layout: Layout,
) -> Path | None:
    snap_dir = layout.snapshots_dir(batch_cfg.batch_id)
    snap_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(batch_yaml_path, layout.batch_yaml_copy_path(batch_cfg.batch_id))

    for rel in (
        batch_cfg.coordination.rules,
        batch_cfg.coordination.decisions,
        batch_cfg.antenna.policy,
        batch_cfg.antenna.decisions,
    ):
        if rel:
            src = resolve_rel_path(rel, batch_dir_base)
            if src.is_file():
                shutil.copy2(src, snap_dir / src.name)

    overrides_src = None
    if batch_cfg.overrides:
        overrides_src = resolve_rel_path(batch_cfg.overrides, batch_dir_base)
        if overrides_src.is_file():
            shutil.copy2(overrides_src, snap_dir / overrides_src.name)
    return overrides_src


def freeze_batch(
    batch_cfg: BatchConfig,
    batch_yaml_path: Path,
    batch_dir_base: Path,
    layout: Layout,
    engine: EngineAdapter,
    source_context: Dict[str, Any] | None = None,
) -> FreezeResult:
    report = run_preflight(batch_cfg, batch_dir_base, layout, engine, source_context=source_context)
    if not report.passed:
        raise PreflightBlockedError(
            len(report.pending_items) + report.unresolved_coordination_count + len(report.empty_plans),
            report.pending_md_path,
        )

    batch_dir = layout.batch_dir(batch_cfg.batch_id)
    batch_dir.mkdir(parents=True, exist_ok=True)
    overrides_src = _snapshot_configs(batch_cfg, batch_yaml_path, batch_dir_base, layout)
    overrides_cfg = load_model(OverridesConfig, overrides_src) if overrides_src else None

    base_curves: Dict[AntennaKey, AntennaCurve] = {
        k: v.curve
        for k, v in report.resolved_antennas.items()
        if v.status == "resolved" and v.curve is not None
    }

    time_dict = batch_cfg.time.model_dump()
    build_opts = dict(batch_cfg.engine.options)
    build_opts["layout_files"] = {
        "antenna_subdir": layout.cfg.layout.antenna_subdir,
        "libraries_subdir": layout.cfg.layout.libraries_subdir,
        "input_workbook_file": layout.cfg.layout.input_workbook_file,
        "run_yaml_file": layout.cfg.layout.run_yaml_file,
        "provenance_file": layout.cfg.layout.provenance_file,
    }

    frozen_pairs: List[FrozenPair] = []
    for plan in report.pair_plans:
        if plan.index in report.empty_plans:
            continue
        pair_root = layout.pair_root(batch_cfg.batch_id, plan.index, plan.interferer, plan.victim)
        frozen_dir = layout.pair_frozen_dir(pair_root)
        raw_dir = layout.pair_raw_dir(pair_root)
        frozen_dir.mkdir(parents=True, exist_ok=True)

        net_a = report.extracted.get((plan.interferer, plan.interferer_ntc_id)) or engine.extract_network(
            plan.interferer, plan.interferer_ntc_id or None, batch_cfg.engine.options
        )
        net_b = report.extracted.get((plan.victim, plan.victim_ntc_id)) or engine.extract_network(
            plan.victim, plan.victim_ntc_id or None, batch_cfg.engine.options
        )

        allowed_keys = set(report.pair_keys.get(plan.index, []))
        pair_curves = {k: c for k, c in base_curves.items() if k in allowed_keys}

        net_a, curves_a, rec_a = apply_overrides(net_a, "interferer", overrides_cfg, pair_curves)
        net_b, curves_b, rec_b = apply_overrides(net_b, "victim", overrides_cfg, curves_a)

        pair_patterns = {
            k: (list(c.phi_deg), list(c.gain_dbi))
            for k, c in curves_b.items()
            if k[0] in (net_a.sat_name, net_b.sat_name)
        }

        provenance = {
            "batch_id": batch_cfg.batch_id,
            "pair_index": plan.index,
            "interferer": net_a.sat_name,
            "interferer_ntc_id": net_a.ntc_id,
            "victim": net_b.sat_name,
            "victim_ntc_id": net_b.ntc_id,
            "time_frozen": time_dict,
            "overrides_applied": [asdict(r) for r in (rec_a + rec_b)],
            "antenna_sources": {
                "|".join(k): {
                    "source_used": report.resolved_antennas[k].source_used,
                    "applied_decisions": report.resolved_antennas[k].applied_decisions,
                }
                for k in allowed_keys
                if k in report.resolved_antennas
            },
        }
        if overrides_src is not None and overrides_src.is_file():
            shutil.copy2(overrides_src, frozen_dir / overrides_src.name)

        engine.build_input(
            interferer=net_a,
            victim=net_b,
            resolved_patterns=pair_patterns,
            time_cfg=time_dict,
            frozen_dir=frozen_dir,
            provenance=provenance,
            options=build_opts,
        )

        frozen_pairs.append(
            FrozenPair(
                index=plan.index,
                pair_dir_name=pair_root.name,
                pair_root=pair_root,
                frozen_dir=frozen_dir,
                raw_dir=raw_dir,
                interferer=net_a.sat_name,
                victim=net_b.sat_name,
            )
        )

    freeze_manifest = layout.freeze_manifest_path(batch_cfg.batch_id)
    freeze_manifest.write_text(
        json.dumps(
            {
                "batch_id": batch_cfg.batch_id,
                "start_utc": batch_cfg.time.start_utc,
                "pairs": [
                    {
                        "index": fp.index,
                        "pair_dir_name": fp.pair_dir_name,
                        "interferer": fp.interferer,
                        "victim": fp.victim,
                        "frozen_dir": str(fp.frozen_dir),
                        "raw_dir": str(fp.raw_dir),
                    }
                    for fp in frozen_pairs
                ],
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    return FreezeResult(batch_id=batch_cfg.batch_id, frozen_pairs=frozen_pairs, manifest_path=freeze_manifest)
