"""Data models for frozen per-pair input bundles."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import List


@dataclass
class FrozenPair:
    index: int
    pair_dir_name: str
    pair_root: Path
    frozen_dir: Path
    raw_dir: Path
    interferer: str
    victim: str


@dataclass
class FreezeResult:
    batch_id: str
    frozen_pairs: List[FrozenPair] = field(default_factory=list)
    manifest_path: Path | None = None
