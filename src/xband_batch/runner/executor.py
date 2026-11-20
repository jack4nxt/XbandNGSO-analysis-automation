"""Batch simulation runner: reads frozen inputs only, writes raw outputs, supports resume."""

from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from ..adapters.base import EngineAdapter
from ..config.loader import resolve_rel_path
from ..config.schemas import BatchConfig
from ..errors import ConfigError
from ..parallel import (
    adapter_for_worker,
    dir_payload_bytes,
    init_worker_process,
    lpt_sort,
    resolve_process_adapter_spec,
    spawn_context,
)
from ..paths import Layout
from .state import BatchState, PairState, file_sha256, utc_now_iso


def _run_pair_in_process(
    spec: str,
    adapter_options: Dict[str, Any],
    frozen_dir_str: str,
    raw_dir_str: str,
    run_options: Dict[str, Any],
) -> Tuple[float, Optional[str]]:
    """Worker entrypoint for process-pool pair simulation."""
    t0 = time.perf_counter()
    try:
        engine = adapter_for_worker(spec, adapter_options)
        Path(raw_dir_str).mkdir(parents=True, exist_ok=True)
        engine.run_pair(Path(frozen_dir_str), Path(raw_dir_str), run_options)
    except Exception as exc:
        return round(time.perf_counter() - t0, 3), f"{type(exc).__name__}: {exc}"
    return round(time.perf_counter() - t0, 3), None


def _load_or_init_state(
    batch_cfg: BatchConfig,
    batch_yaml_path: Path,
    batch_dir_base: Path,
    layout: Layout,
) -> tuple[BatchState, Path]:
    freeze_manifest_path = layout.freeze_manifest_path(batch_cfg.batch_id)
    if not freeze_manifest_path.is_file():
        raise ConfigError(
            freeze_manifest_path,
            "file",
            f"{freeze_manifest_path.name} not found; run `xband-batch freeze` first",
        )
    freeze_data = json.loads(freeze_manifest_path.read_text(encoding="utf-8"))
    state_path = layout.batch_state_path(batch_cfg.batch_id)

    hashes: Dict[str, str | None] = {
        "batch_yaml": file_sha256(batch_yaml_path),
        "antenna_decisions": file_sha256(resolve_rel_path(batch_cfg.antenna.decisions, batch_dir_base)),
        "overrides": (
            file_sha256(resolve_rel_path(batch_cfg.overrides, batch_dir_base))
            if batch_cfg.overrides
            else None
        ),
    }

    if state_path.is_file() and batch_cfg.runner.resume:
        state = BatchState.load(state_path)
        state.config_hashes = hashes
        return state, state_path

    pairs = [
        PairState(
            index=p["index"],
            pair_dir_name=p["pair_dir_name"],
            interferer=p["interferer"],
            victim=p["victim"],
            frozen_dir=p["frozen_dir"],
            raw_dir=p["raw_dir"],
        )
        for p in freeze_data.get("pairs", [])
    ]
    state = BatchState(
        batch_id=batch_cfg.batch_id,
        start_utc=batch_cfg.time.start_utc,
        config_hashes=hashes,
        pairs=pairs,
    )
    state.save(state_path)
    return state, state_path


class BatchRunner:
    def __init__(self, engine: EngineAdapter):
        self.engine = engine
        self._lock = threading.Lock()

    def run(
        self,
        batch_cfg: BatchConfig,
        batch_yaml_path: Path,
        batch_dir_base: Path,
        layout: Layout,
    ) -> BatchState:
        state, state_path = _load_or_init_state(batch_cfg, batch_yaml_path, batch_dir_base, layout)
        todo = [
            p
            for p in state.pairs
            if not (batch_cfg.runner.resume and p.status == "done")
        ]
        if not todo:
            return state

        todo = lpt_sort(todo, lambda p: dir_payload_bytes(Path(p.frozen_dir)))

        opts = dict(batch_cfg.engine.options)
        opts["layout_files"] = {
            "run_yaml_file": layout.cfg.layout.run_yaml_file,
            "provenance_file": layout.cfg.layout.provenance_file,
        }

        def _exec_one(pair: PairState) -> None:
            t0 = time.perf_counter()
            with self._lock:
                pair.status = "running"
                pair.started_utc = utc_now_iso()
                pair.error = None
                state.save(state_path)
            try:
                Path(pair.raw_dir).mkdir(parents=True, exist_ok=True)
                self.engine.run_pair(Path(pair.frozen_dir), Path(pair.raw_dir), opts)
            except Exception as exc:
                with self._lock:
                    pair.status = "failed"
                    pair.finished_utc = utc_now_iso()
                    pair.duration_s = round(time.perf_counter() - t0, 3)
                    pair.error = f"{type(exc).__name__}: {exc}"
                    state.save(state_path)
                if not batch_cfg.runner.continue_on_error:
                    raise
            else:
                with self._lock:
                    pair.status = "done"
                    pair.finished_utc = utc_now_iso()
                    pair.duration_s = round(time.perf_counter() - t0, 3)
                    state.save(state_path)

        workers = batch_cfg.runner.max_workers
        proc_spec = (
            resolve_process_adapter_spec(self.engine, batch_cfg.engine.engine_adapter)
            if workers > 1 and batch_cfg.runner.executor == "process"
            else None
        )

        if workers == 1:
            for pair in todo:
                _exec_one(pair)
        elif proc_spec is not None:
            with ProcessPoolExecutor(
                max_workers=workers,
                mp_context=spawn_context(),
                initializer=init_worker_process,
            ) as pool:
                future_to_pair = {}
                for pair in todo:
                    pair.status = "running"
                    pair.started_utc = utc_now_iso()
                    pair.error = None
                    fut = pool.submit(
                        _run_pair_in_process,
                        proc_spec,
                        batch_cfg.engine.options,
                        pair.frozen_dir,
                        pair.raw_dir,
                        opts,
                    )
                    future_to_pair[fut] = pair
                state.save(state_path)

                for fut in as_completed(future_to_pair):
                    pair = future_to_pair[fut]
                    dur_s, err = fut.result()
                    pair.finished_utc = utc_now_iso()
                    pair.duration_s = dur_s
                    pair.error = err
                    pair.status = "done" if err is None else "failed"
                    state.save(state_path)
                    if err is not None and not batch_cfg.runner.continue_on_error:
                        for pending_fut in future_to_pair:
                            pending_fut.cancel()
                        raise RuntimeError(f"{pair.pair_dir_name}: {err}")
        else:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = [pool.submit(_exec_one, pair) for pair in todo]
                for fut in as_completed(futures):
                    fut.result()

        return state
