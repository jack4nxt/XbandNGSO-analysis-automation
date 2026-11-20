"""Shared process-pool initialization, adapter resolution, and LPT scheduling helpers."""

from __future__ import annotations

import importlib
import json
import multiprocessing as mp
import os
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple, TypeVar

from .adapters.loader import load_adapter

T = TypeVar("T")

_SINGLE_THREAD_ENV = {
    "OMP_NUM_THREADS": "1",
    "OPENBLAS_NUM_THREADS": "1",
    "MKL_NUM_THREADS": "1",
    "VECLIB_MAXIMUM_THREADS": "1",
    "NUMEXPR_NUM_THREADS": "1",
    "MPLBACKEND": "Agg",
}

_WORKER_ADAPTER_CACHE: Dict[Tuple[str, str], Any] = {}


def init_worker_process() -> None:
    """Configure spawned worker processes to avoid BLAS thread oversubscription and GUI backends."""
    for key, val in _SINGLE_THREAD_ENV.items():
        os.environ.setdefault(key, val)


def spawn_context() -> mp.context.BaseContext:
    """Return a spawn multiprocessing context safe across macOS and Linux."""
    return mp.get_context("spawn")


def resolve_process_adapter_spec(engine: Any, configured_spec: str) -> Optional[str]:
    """Return `configured_spec` when `engine` matches that class; None for in-memory test doubles."""
    if ":" not in configured_spec:
        return None
    mod_name, cls_name = configured_spec.split(":", 1)
    try:
        cls = getattr(importlib.import_module(mod_name), cls_name, None)
    except ImportError:
        return None
    if cls is not None and isinstance(engine, cls):
        return configured_spec
    return None


def adapter_for_worker(spec: str, options: Dict[str, Any]) -> Any:
    """Return a per-process cached EngineAdapter instance."""
    cache_key = (spec, json.dumps(options or {}, sort_keys=True, default=str))
    if cache_key not in _WORKER_ADAPTER_CACHE:
        _WORKER_ADAPTER_CACHE[cache_key] = load_adapter(spec, options)
    return _WORKER_ADAPTER_CACHE[cache_key]


def dir_payload_bytes(path: Path) -> int:
    """Sum regular file sizes under `path`, skipping AppleDouble (`._*`) metadata files."""
    if not path.is_dir():
        return 0
    total = 0
    for item in path.rglob("*"):
        if item.name.startswith("._"):
            continue
        if item.is_file():
            try:
                total += item.stat().st_size
            except OSError:
                continue
    return total


def lpt_sort(items: Iterable[T], cost_fn: Callable[[T], int | float]) -> List[T]:
    """Order tasks descending by estimated cost (Longest Processing Time first)."""
    return sorted(items, key=cost_fn, reverse=True)
