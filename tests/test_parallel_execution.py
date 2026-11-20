"""Tests for multi-process 4-worker BatchRunner and parallel post-processing + LPT scheduling."""

from __future__ import annotations

import csv
import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List

import yaml

from xband_batch.adapters import load_adapter
from xband_batch.config import BatchConfig, PathsConfig, PostBatchConfig, load_model
from xband_batch.parallel import dir_payload_bytes, lpt_sort
from xband_batch.paths import Layout
from xband_batch.post import run_batch_post
from xband_batch.runner import BatchRunner, BatchState, PairState


class SpawnableTestAdapter:
    """Importable adapter used to test real ProcessPoolExecutor (`spawn`) across run and post."""

    def __init__(self, options: Dict[str, Any] | None = None):
        self.options = dict(options or {})

    def run_pair(self, frozen_dir: Path, raw_dir: Path, options: Dict[str, Any]) -> Dict[str, Any]:
        meta = json.loads((frozen_dir / "frozen_meta.json").read_text(encoding="utf-8"))
        time.sleep(0.05)
        if meta["victim"] in set(self.options.get("fail_victims", [])):
            raise RuntimeError(f"boom on {meta['victim']}")
        raw_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            **meta,
            "run_pid": os.getpid(),
            "omp_threads": os.environ.get("OMP_NUM_THREADS"),
            "mpl_backend": os.environ.get("MPLBACKEND"),
        }
        (raw_dir / "raw_marker.json").write_text(json.dumps(payload), encoding="utf-8")
        return {"raw_dir": str(raw_dir)}

    def run_stats(
        self,
        raw_dir: Path,
        post_out_dir: Path,
        post_cfg: Dict[str, Any],
        report_cfg: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        meta = json.loads((raw_dir / "raw_marker.json").read_text(encoding="utf-8"))
        time.sleep(0.05)
        post_out_dir.mkdir(parents=True, exist_ok=True)
        (post_out_dir / "post_marker.json").write_text(
            json.dumps({"post_pid": os.getpid(), "victim": meta["victim"]}),
            encoding="utf-8",
        )
        return [
            {
                "scenario_id": f"DL_SZU-1_vs_{meta['victim']}",
                "direction": "DL",
                "criteria_set": "SA1027-6_8025-8400",
                "pass": meta["victim"] != "V1",
                "max_exceed_percent": 2.0 if meta["victim"] == "V1" else 0.1,
                "min_margin_db": -1.5 if meta["victim"] == "V1" else 4.0,
                "worst_link": f"ES_A -> ES_{meta['victim']}",
            }
        ]


def test_lpt_sort_and_dir_payload_ignores_appledouble(tmp_path: Path) -> None:
    d_small = tmp_path / "small"
    d_big = tmp_path / "big"
    d_small.mkdir()
    d_big.mkdir()
    (d_small / "a.bin").write_bytes(b"x" * 100)
    (d_small / "._a.bin").write_bytes(b"y" * 99999)
    (d_big / "b.bin").write_bytes(b"z" * 500)

    assert dir_payload_bytes(d_small) == 100
    assert dir_payload_bytes(d_big) == 500
    ordered = lpt_sort([d_small, d_big], dir_payload_bytes)
    assert ordered == [d_big, d_small]


def test_multiprocess_run_and_post_with_four_workers(sample_configs: dict[str, Path]) -> None:
    paths_cfg = load_model(PathsConfig, sample_configs["paths"])
    layout = Layout(paths_cfg)

    batch_data = yaml.safe_load(sample_configs["batch"].read_text(encoding="utf-8"))
    batch_data["engine"]["engine_adapter"] = "tests.test_parallel_execution:SpawnableTestAdapter"
    batch_data["engine"]["options"] = {"fail_victims": ["V4"]}
    batch_data["runner"] = {
        "max_workers": 4,
        "executor": "process",
        "resume": True,
        "continue_on_error": True,
    }
    batch_path = sample_configs["cfg_dir"] / "batch_mp.yaml"
    batch_path.write_text(yaml.safe_dump(batch_data, sort_keys=False), encoding="utf-8")
    batch_cfg = load_model(BatchConfig, batch_path)

    post_data = yaml.safe_load(sample_configs["post"].read_text(encoding="utf-8"))
    post_data["max_workers"] = 4
    post_data["executor"] = "process"
    post_path = sample_configs["cfg_dir"] / "post_mp.yaml"
    post_path.write_text(yaml.safe_dump(post_data, sort_keys=False), encoding="utf-8")
    post_cfg = load_model(PostBatchConfig, post_path)

    # Seed freeze_manifest + 5 frozen pairs
    batch_dir = layout.batch_dir(batch_cfg.batch_id)
    batch_dir.mkdir(parents=True, exist_ok=True)
    manifest_pairs = []
    for idx in range(1, 6):
        victim = f"V{idx}"
        pair_root = layout.pair_root(batch_cfg.batch_id, idx, "SZU-1", victim)
        frozen_dir = layout.pair_frozen_dir(pair_root)
        raw_dir = layout.pair_raw_dir(pair_root)
        frozen_dir.mkdir(parents=True, exist_ok=True)
        (frozen_dir / "frozen_meta.json").write_text(
            json.dumps({"interferer": "SZU-1", "victim": victim}),
            encoding="utf-8",
        )
        # Make V5 biggest so LPT schedules it first
        (frozen_dir / "payload.bin").write_bytes(b"0" * (idx * 100))
        manifest_pairs.append(
            {
                "index": idx,
                "pair_dir_name": pair_root.name,
                "interferer": "SZU-1",
                "victim": victim,
                "frozen_dir": str(frozen_dir),
                "raw_dir": str(raw_dir),
            }
        )
    layout.freeze_manifest_path(batch_cfg.batch_id).write_text(
        json.dumps({"batch_id": batch_cfg.batch_id, "pairs": manifest_pairs}),
        encoding="utf-8",
    )

    engine = load_adapter(batch_cfg.engine.engine_adapter, batch_cfg.engine.options)
    parent_pid = os.getpid()

    # 1. Run with 4 spawned worker processes (V4 intentionally fails)
    state = BatchRunner(engine).run(batch_cfg, batch_path, sample_configs["cfg_dir"], layout)
    assert isinstance(state, BatchState)
    statuses = {p.victim: p.status for p in state.pairs}
    assert statuses == {"V1": "done", "V2": "done", "V3": "done", "V4": "failed", "V5": "done"}
    assert "boom on V4" in (next(p for p in state.pairs if p.victim == "V4").error or "")

    run_pids = set()
    for p in state.pairs:
        if p.status == "done":
            marker = json.loads((Path(p.raw_dir) / "raw_marker.json").read_text(encoding="utf-8"))
            run_pids.add(marker["run_pid"])
            assert marker["omp_threads"] == "1"
            assert marker["mpl_backend"] == "Agg"
    assert parent_pid not in run_pids
    assert len(run_pids) >= 2

    # 2. Parallel 4-worker post on the 4 done pairs
    post_dir = run_batch_post(batch_cfg, post_cfg, post_path, layout, engine)
    assert (post_dir / "batch_summary.xlsx").is_file()
    verdict_csv = post_dir / "batch_verdict.csv"
    assert verdict_csv.is_file()

    with verdict_csv.open(encoding="utf-8-sig") as fh:
        rows = list(csv.DictReader(fh))
    assert len(rows) == 4
    assert rows[0]["victim"] == "V1"
    assert rows[0]["pass"] == "False"

    post_pids = set()
    for p in state.pairs:
        if p.status == "done":
            pm = json.loads(
                (post_dir / "pairs" / p.pair_dir_name / "post_marker.json").read_text(encoding="utf-8")
            )
            post_pids.add(pm["post_pid"])
    assert parent_pid not in post_pids
    assert len(post_pids) >= 2
