"""Standalone batch post-processing: reads raw outputs only and builds cross-pair summary."""

from __future__ import annotations

import shutil
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List

import pandas as pd

from ..adapters.base import EngineAdapter
from ..config.schemas import BatchConfig, PostBatchConfig
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
from ..runner.state import BatchState, PairState


def _run_post_in_process(
    spec: str,
    adapter_options: Dict[str, Any],
    raw_dir_str: str,
    post_out_dir_str: str,
    engine_post_dict: Dict[str, Any],
    report_dict: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """Worker entrypoint for process-pool per-pair post-processing."""
    engine = adapter_for_worker(spec, adapter_options)
    post_out_dir = Path(post_out_dir_str)
    post_out_dir.mkdir(parents=True, exist_ok=True)
    return engine.run_stats(
        raw_dir=Path(raw_dir_str),
        post_out_dir=post_out_dir,
        post_cfg=engine_post_dict,
        report_cfg=report_dict,
    )


def _enrich_rows(pair: PairState, rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for r in rows:
        enriched = dict(r)
        enriched.setdefault("pair_id", pair.pair_dir_name)
        enriched.setdefault("interferer", pair.interferer)
        enriched.setdefault("victim", pair.victim)
        out.append(enriched)
    return out


def _collect_post_futures(
    future_to_pair: Dict[Any, PairState],
    continue_on_error: bool,
) -> Dict[int, List[Dict[str, Any]]]:
    rows_by_index: Dict[int, List[Dict[str, Any]]] = {}
    for fut in as_completed(future_to_pair):
        pair = future_to_pair[fut]
        try:
            rows_by_index[pair.index] = _enrich_rows(pair, fut.result())
        except Exception:
            if not continue_on_error:
                for pending_fut in future_to_pair:
                    pending_fut.cancel()
                raise
            rows_by_index[pair.index] = []
    return rows_by_index


def run_batch_post(
    batch_cfg: BatchConfig,
    post_cfg: PostBatchConfig,
    post_yaml_path: Path,
    layout: Layout,
    engine: EngineAdapter,
) -> Path:
    state_path = layout.batch_state_path(batch_cfg.batch_id)
    if not state_path.is_file():
        raise ConfigError(state_path, "file", "batch_state.json not found; run `xband-batch run` first")
    state = BatchState.load(state_path)

    post_dir = layout.post_run_dir(batch_cfg.batch_id, post_cfg.post_name)
    pairs_out = post_dir / layout.cfg.layout.pairs_subdir
    pairs_out.mkdir(parents=True, exist_ok=True)
    shutil.copy2(post_yaml_path, post_dir / Path(post_yaml_path).name)

    engine_post_dict = post_cfg.engine_post.model_dump()
    engine_post_dict["engine_options"] = dict(batch_cfg.engine.options)
    report_dict = post_cfg.report.model_dump()

    done_pairs = [p for p in state.pairs if p.status == "done"]
    ordered_indices = [p.index for p in done_pairs]
    scheduled = lpt_sort(done_pairs, lambda p: dir_payload_bytes(Path(p.raw_dir)))
    rows_by_index: Dict[int, List[Dict[str, Any]]] = {}

    workers = min(post_cfg.max_workers, max(1, len(scheduled)))
    proc_spec = (
        resolve_process_adapter_spec(engine, batch_cfg.engine.engine_adapter)
        if workers > 1 and post_cfg.executor == "process"
        else None
    )

    if workers == 1:
        for pair in scheduled:
            pair_post_dir = pairs_out / pair.pair_dir_name
            pair_post_dir.mkdir(parents=True, exist_ok=True)
            try:
                rows = engine.run_stats(
                    raw_dir=Path(pair.raw_dir),
                    post_out_dir=pair_post_dir,
                    post_cfg=engine_post_dict,
                    report_cfg=report_dict,
                )
                rows_by_index[pair.index] = _enrich_rows(pair, rows)
            except Exception:
                if not post_cfg.continue_on_error:
                    raise
                rows_by_index[pair.index] = []
    elif proc_spec is not None:
        with ProcessPoolExecutor(
            max_workers=workers,
            mp_context=spawn_context(),
            initializer=init_worker_process,
        ) as pool:
            future_to_pair = {}
            for pair in scheduled:
                pair_post_dir = pairs_out / pair.pair_dir_name
                pair_post_dir.mkdir(parents=True, exist_ok=True)
                fut = pool.submit(
                    _run_post_in_process,
                    proc_spec,
                    batch_cfg.engine.options,
                    pair.raw_dir,
                    str(pair_post_dir),
                    engine_post_dict,
                    report_dict,
                )
                future_to_pair[fut] = pair

            rows_by_index = _collect_post_futures(future_to_pair, post_cfg.continue_on_error)
    else:
        def _exec_post(pair: PairState) -> List[Dict[str, Any]]:
            pair_post_dir = pairs_out / pair.pair_dir_name
            pair_post_dir.mkdir(parents=True, exist_ok=True)
            return engine.run_stats(
                raw_dir=Path(pair.raw_dir),
                post_out_dir=pair_post_dir,
                post_cfg=engine_post_dict,
                report_cfg=report_dict,
            )

        with ThreadPoolExecutor(max_workers=workers) as pool:
            future_to_pair = {pool.submit(_exec_post, pair): pair for pair in scheduled}
            rows_by_index = _collect_post_futures(future_to_pair, post_cfg.continue_on_error)

    all_rows: List[Dict[str, Any]] = []
    for idx in ordered_indices:
        all_rows.extend(rows_by_index.get(idx, []))

    df = pd.DataFrame(all_rows)
    if not df.empty:
        sort_cols = [c for c in post_cfg.summary.sort_by if c in df.columns]
        if sort_cols:
            df = df.sort_values(sort_cols, kind="stable").reset_index(drop=True)
        selected_cols = [c for c in post_cfg.summary.columns if c in df.columns]
        summary_df = df[selected_cols] if selected_cols else df
    else:
        summary_df = pd.DataFrame(columns=post_cfg.summary.columns)

    verdict_csv = post_dir / layout.cfg.layout.batch_verdict_file
    summary_xlsx = post_dir / layout.cfg.layout.batch_summary_file
    df.to_csv(verdict_csv, index=False, encoding="utf-8-sig")
    with pd.ExcelWriter(summary_xlsx, engine="openpyxl") as xw:
        summary_df.to_excel(xw, sheet_name="BATCH_SUMMARY", index=False)
        if not df.empty:
            df.to_excel(xw, sheet_name="ALL_DETAIL", index=False)

    return post_dir
