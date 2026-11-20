"""End-to-end contract test across parse-list -> preflight -> freeze -> run -> post."""

from __future__ import annotations

from pathlib import Path

import openpyxl

from tests.conftest import FakeCatalogAdapter, FakeEngineAdapter, make_network
from xband_batch.adapters.base import NetworkCandidate
from xband_batch.config import BatchConfig, PathsConfig, PostBatchConfig, load_model
from xband_batch.coordination import run_parse_list
from xband_batch.freeze import freeze_batch
from xband_batch.paths import Layout
from xband_batch.post import run_batch_post
from xband_batch.preflight import run_preflight
from xband_batch.runner import BatchRunner


def test_full_pipeline_contract_with_resume_and_post(sample_configs: dict[str, Path]) -> None:
    paths_cfg = load_model(PathsConfig, sample_configs["paths"])
    batch_cfg = load_model(BatchConfig, sample_configs["batch"])
    post_cfg = load_model(PostBatchConfig, sample_configs["post"])
    layout = Layout(paths_cfg)

    # Create a clean coordination Excel containing only READY networks so preflight passes
    clean_xlsx = sample_configs["cfg_dir"] / "clean_coord.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "List"
    ws.append(["Frequency Band", "Operator", "Satellite Network", "Notice ID", "Station of CHN", "受影响原因"])
    ws.append(["8025-8400", "OrgA", "VICTIM-1 (NGSO)\nVICTIM-2 (NGSO)", None, "KASHI", "频率重叠"])
    wb.save(clean_xlsx)

    batch_data = batch_cfg.model_dump()
    batch_data["coordination"]["inputs"] = [{"path": str(clean_xlsx), "kind": "legacy_excel"}]
    batch_cfg = BatchConfig.model_validate(batch_data)

    szu = make_network("SZU-1", ntc_id=1, direction="DL")
    v1 = make_network("VICTIM-1", ntc_id=2, direction="DL")
    v2 = make_network("VICTIM-2", ntc_id=3, direction="DL")

    catalog = FakeCatalogAdapter(
        candidates_by_name={
            "SZU-1": [NetworkCandidate("SZU-1", 1, "CHN", "NGSO")],
            "VICTIM-1": [NetworkCandidate("VICTIM-1", 2, "CHN", "NGSO")],
            "VICTIM-2": [NetworkCandidate("VICTIM-2", 3, "CHN", "NGSO")],
        },
        extracts_by_ntc={1: szu, 2: v1, 3: v2},
    )
    engine = FakeEngineAdapter(
        extracts_by_name={"SZU-1": szu, "VICTIM-1": v1, "VICTIM-2": v2},
        fail_victims={"VICTIM-2"},
    )
    def _dummy_eval(_model_id, req, _policy, _params):
        gmax = req.declared_gmax_dbi if req.declared_gmax_dbi is not None else 20.0
        half_bw = (req.declared_beamwidth_deg / 2.0) if req.declared_beamwidth_deg else 5.0
        return ([0.0, half_bw, 180.0], [gmax, gmax - 3.0, -10.0])

    source_ctx = {"model_evaluator": _dummy_eval}

    # 1. parse-list
    coord_report = run_parse_list(batch_cfg, sample_configs["cfg_dir"], layout, catalog)
    assert coord_report.counts_by_category["READY_CO_DIR"] == 2

    # 2. preflight
    pre = run_preflight(batch_cfg, sample_configs["cfg_dir"], layout, engine, source_context=source_ctx)
    assert pre.passed

    # 3. freeze
    frz = freeze_batch(
        batch_cfg,
        sample_configs["batch"],
        sample_configs["cfg_dir"],
        layout,
        engine,
        source_context=source_ctx,
    )
    assert len(frz.frozen_pairs) == 2

    # 4. run (VICTIM-2 fails, VICTIM-1 succeeds)
    state1 = BatchRunner(engine).run(batch_cfg, sample_configs["batch"], sample_configs["cfg_dir"], layout)
    assert [p.status for p in state1.pairs] == ["done", "failed"]
    assert engine.run_calls == ["VICTIM-1", "VICTIM-2"]

    # 5. resume after fixing VICTIM-2 -> VICTIM-1 is NOT re-run
    engine.fail_victims.clear()
    engine.run_calls.clear()
    state2 = BatchRunner(engine).run(batch_cfg, sample_configs["batch"], sample_configs["cfg_dir"], layout)
    assert [p.status for p in state2.pairs] == ["done", "done"]
    assert engine.run_calls == ["VICTIM-2"]

    # 6. standalone post
    post_dir = run_batch_post(batch_cfg, post_cfg, sample_configs["post"], layout, engine)
    assert (post_dir / "batch_summary.xlsx").is_file()
    assert (post_dir / "batch_verdict.csv").is_file()
