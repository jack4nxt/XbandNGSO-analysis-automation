"""Data root resolution and batch directory layout (zero hardcoded filesystem paths)."""

from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from pathlib import Path

from .config.schemas import PathsConfig
from .errors import PathUnavailableError


def _load_env_file(env_file: str) -> None:
    path = Path(env_file).expanduser()
    if not path.is_file():
        raise PathUnavailableError(f"env_file {path} not found")
    try:
        from dotenv import load_dotenv
    except ImportError as exc:  # pragma: no cover - depends on environment
        raise PathUnavailableError("paths.env_file is set but python-dotenv is not installed") from exc
    load_dotenv(path)


class Layout:
    """Resolves all data paths from PathsConfig and the configured environment variable."""

    def __init__(self, cfg: PathsConfig):
        self.cfg = cfg
        if cfg.env_file and not os.getenv(cfg.data_root_env):
            _load_env_file(cfg.env_file)
        raw_root = os.getenv(cfg.data_root_env)
        if not raw_root:
            raise PathUnavailableError(
                f"environment variable {cfg.data_root_env!r} is not set; "
                "cannot resolve external data root"
            )
        root = Path(raw_root).expanduser()
        if not root.exists():
            raise PathUnavailableError(
                f"data root {root} (from {cfg.data_root_env}) does not exist; "
                "check external drive mount"
            )
        self.data_root = root
        self.outputs_root = root / cfg.dirs.outputs
        self.cache_root = root / cfg.dirs.cache
        self.raw_inputs_root = root / cfg.dirs.raw_inputs
        self._sanitize_re = re.compile(cfg.layout.name_sanitize_regex)

    # ------------------------------------------------------------------ names
    def sanitize(self, name: str) -> str:
        cleaned = self._sanitize_re.sub(self.cfg.layout.name_sanitize_char, name.strip())
        return cleaned.strip(self.cfg.layout.name_sanitize_char) or "unnamed"

    def timestamp(self) -> str:
        return datetime.now(timezone.utc).strftime(self.cfg.layout.timestamp_format)

    # ------------------------------------------------------------------ batch level
    def batch_dir(self, batch_id: str) -> Path:
        name = self.cfg.layout.batch_dir.format(batch_id=self.sanitize(batch_id))
        return self.outputs_root / name

    def coordination_table_path(self, batch_id: str) -> Path:
        return self.batch_dir(batch_id) / self.cfg.layout.coordination_table_file

    def coordination_context_path(self, batch_id: str) -> Path:
        return self.batch_dir(batch_id) / self.cfg.layout.coordination_context_file

    def validation_report_path(self, batch_id: str) -> Path:
        return self.batch_dir(batch_id) / self.cfg.layout.validation_report_file

    def batch_state_path(self, batch_id: str) -> Path:
        return self.batch_dir(batch_id) / self.cfg.layout.batch_state_file

    def freeze_manifest_path(self, batch_id: str) -> Path:
        return self.batch_dir(batch_id) / self.cfg.layout.freeze_manifest_file

    def batch_yaml_copy_path(self, batch_id: str) -> Path:
        return self.batch_dir(batch_id) / self.cfg.layout.batch_yaml_copy_file

    def snapshots_dir(self, batch_id: str) -> Path:
        return self.batch_dir(batch_id) / self.cfg.layout.snapshots_subdir

    def preflight_dir(self, batch_id: str) -> Path:
        return self.batch_dir(batch_id) / self.cfg.layout.preflight_dir

    # ------------------------------------------------------------------ pair level
    def pair_dir_name(self, index: int, interferer: str, victim: str) -> str:
        idx = str(index).zfill(self.cfg.layout.idx_width)
        return self.cfg.layout.pair_dir.format(
            idx=idx,
            interferer=self.sanitize(interferer),
            victim=self.sanitize(victim),
        )

    def pair_root(self, batch_id: str, index: int, interferer: str, victim: str) -> Path:
        return (
            self.batch_dir(batch_id)
            / self.cfg.layout.pairs_subdir
            / self.pair_dir_name(index, interferer, victim)
        )

    def pair_frozen_dir(self, pair_root: Path) -> Path:
        return pair_root / self.cfg.layout.frozen_subdir

    def pair_raw_dir(self, pair_root: Path) -> Path:
        return pair_root / self.cfg.layout.raw_subdir

    # ------------------------------------------------------------------ post level
    def post_run_dir(self, batch_id: str, post_name: str, ts: str | None = None) -> Path:
        stamp = ts or self.timestamp()
        folder = self.cfg.layout.post_dir.format(
            post_name=self.sanitize(post_name),
            ts=stamp,
        )
        return self.batch_dir(batch_id) / self.cfg.layout.post_runs_subdir / folder
