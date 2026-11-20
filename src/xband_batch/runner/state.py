"""Batch execution state persistence (`batch_state.json`) for resume and audit."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Literal, Optional

PairStatus = Literal["pending", "running", "done", "failed", "skipped"]


def file_sha256(path: Path | None) -> Optional[str]:
    if path is None or not path.is_file():
        return None
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


@dataclass
class PairState:
    index: int
    pair_dir_name: str
    interferer: str
    victim: str
    frozen_dir: str
    raw_dir: str
    status: PairStatus = "pending"
    started_utc: Optional[str] = None
    finished_utc: Optional[str] = None
    duration_s: Optional[float] = None
    error: Optional[str] = None


@dataclass
class BatchState:
    batch_id: str
    start_utc: str
    config_hashes: Dict[str, Optional[str]] = field(default_factory=dict)
    pairs: List[PairState] = field(default_factory=list)

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "batch_id": self.batch_id,
            "start_utc": self.start_utc,
            "config_hashes": self.config_hashes,
            "pairs": [asdict(p) for p in self.pairs],
        }
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> "BatchState":
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            batch_id=data["batch_id"],
            start_utc=data["start_utc"],
            config_hashes=data.get("config_hashes", {}),
            pairs=[PairState(**p) for p in data.get("pairs", [])],
        )


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
