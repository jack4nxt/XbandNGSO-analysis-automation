"""Tests for strict configuration schemas, env expansion, and TODAY rejection."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from xband_batch.config import (
    AntennaDecisionsConfig,
    AntennaPolicyConfig,
    BatchConfig,
    CoordinationDecisionsConfig,
    CoordinationRulesConfig,
    OverridesConfig,
    PathsConfig,
    PostBatchConfig,
    load_model,
)
from xband_batch.errors import ConfigError, PathUnavailableError
from xband_batch.paths import Layout


def test_forbid_today_start_utc(sample_configs: dict[str, Path]) -> None:
    batch_path = sample_configs["batch"]
    data = yaml.safe_load(batch_path.read_text(encoding="utf-8"))
    data["time"]["start_utc"] = "TODAY"
    bad_path = batch_path.with_name("bad_today.yaml")
    bad_path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")

    with pytest.raises(ConfigError) as exc:
        load_model(BatchConfig, bad_path)
    assert "TODAY" in str(exc.value)


def test_missing_required_field_fails_explicitly(sample_configs: dict[str, Path]) -> None:
    batch_path = sample_configs["batch"]
    data = yaml.safe_load(batch_path.read_text(encoding="utf-8"))
    del data["evaluated_role"]
    bad_path = batch_path.with_name("bad_missing.yaml")
    bad_path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")

    with pytest.raises(ConfigError) as exc:
        load_model(BatchConfig, bad_path)
    assert "evaluated_role" in str(exc.value)


def test_missing_data_root_env_raises(sample_configs: dict[str, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("XBAND_TEST_DATA_ROOT", raising=False)
    paths_cfg = load_model(PathsConfig, sample_configs["paths"])
    with pytest.raises(PathUnavailableError):
        Layout(paths_cfg)


def test_example_configs_match_schemas(monkeypatch: pytest.MonkeyPatch) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    cfg_dir = repo_root / "configs"
    monkeypatch.setenv("XBAND_COORD_INPUT_FILE", "/tmp/coord.xlsx")
    monkeypatch.setenv("XBAND_NGSO_REPO", "/tmp/xband-ngso")
    monkeypatch.setenv("XBAND_NGSO_DATA_ROOT", "/tmp/xband-data")

    assert load_model(PathsConfig, cfg_dir / "paths.example.yaml")
    assert load_model(BatchConfig, cfg_dir / "batch.example.yaml")
    assert load_model(CoordinationRulesConfig, cfg_dir / "coordination_rules.example.yaml")
    assert load_model(CoordinationDecisionsConfig, cfg_dir / "coordination_decisions.example.yaml")
    assert load_model(AntennaPolicyConfig, cfg_dir / "antenna_policy.example.yaml")
    assert load_model(AntennaDecisionsConfig, cfg_dir / "antenna_decisions.example.yaml")
    assert load_model(OverridesConfig, cfg_dir / "overrides.example.yaml")
    assert load_model(PostBatchConfig, cfg_dir / "post.example.yaml")
