"""Shared pytest fixtures: temporary data root, fake adapters, and config builders."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import openpyxl
import pytest
import yaml

from xband_batch.adapters.base import (
    CarrierDescriptor,
    EarthStationDescriptor,
    NetworkCandidate,
    NetworkExtract,
    OrbitDescriptor,
)
from xband_batch.config import load_model
from xband_batch.config.schemas import PathsConfig
from xband_batch.paths import Layout


class FakeCatalogAdapter:
    def __init__(
        self,
        candidates_by_name: Dict[str, List[NetworkCandidate]],
        extracts_by_ntc: Dict[int, NetworkExtract],
    ):
        self.candidates_by_name = candidates_by_name
        self.extracts_by_ntc = extracts_by_ntc

    def find_networks(
        self,
        name: str,
        strategy: str,
        adm: Optional[str] = None,
    ) -> List[NetworkCandidate]:
        key = name.upper().strip()
        if strategy == "exact":
            cands = self.candidates_by_name.get(key, [])
        else:
            stripped = "".join(ch for ch in key if ch.isalnum())
            cands = []
            for k, v in self.candidates_by_name.items():
                if "".join(ch for ch in k if ch.isalnum()) == stripped:
                    cands.extend(v)
        if adm:
            cands = [c for c in cands if c.adm == adm]
        return list(cands)

    def describe_network(
        self,
        ntc_id: int,
        bands: List[Dict[str, Any]],
        country_filter: Optional[str] = None,
    ) -> NetworkExtract:
        net = self.extracts_by_ntc[ntc_id]
        if country_filter:
            filtered = [s for s in net.stations if s.country == country_filter]
            return NetworkExtract(
                sat_name=net.sat_name,
                ntc_id=net.ntc_id,
                adm=net.adm,
                orbit_type=net.orbit_type,
                planes=list(net.planes),
                carriers=list(net.carriers),
                stations=filtered,
                metadata=dict(net.metadata),
            )
        return net


class FakeEngineAdapter:
    def __init__(self, extracts_by_name: Dict[str, NetworkExtract], fail_victims: set[str] | None = None):
        self.extracts_by_name = extracts_by_name
        self.fail_victims = fail_victims or set()
        self.run_calls: List[str] = []

    def extract_network(
        self,
        sat_name: str,
        ntc_id: Optional[int],
        options: Dict[str, Any],
    ) -> NetworkExtract:
        return self.extracts_by_name[sat_name.upper()]

    def build_input(
        self,
        interferer: NetworkExtract,
        victim: NetworkExtract,
        resolved_patterns: Dict[Tuple[str, str, str, str], Tuple[List[float], List[float]]],
        time_cfg: Dict[str, Any],
        frozen_dir: Path,
        provenance: Dict[str, Any],
        options: Dict[str, Any],
    ) -> Dict[str, Any]:
        frozen_dir.mkdir(parents=True, exist_ok=True)
        (frozen_dir / "provenance.json").write_text(
            json.dumps(provenance, indent=2, default=str),
            encoding="utf-8",
        )
        (frozen_dir / "frozen_meta.json").write_text(
            json.dumps({"interferer": interferer.sat_name, "victim": victim.sat_name, "time": time_cfg}),
            encoding="utf-8",
        )
        return {"frozen_dir": str(frozen_dir)}

    def run_pair(
        self,
        frozen_dir: Path,
        raw_dir: Path,
        options: Dict[str, Any],
    ) -> Dict[str, Any]:
        meta = json.loads((frozen_dir / "frozen_meta.json").read_text(encoding="utf-8"))
        victim = meta["victim"]
        self.run_calls.append(victim)
        if victim in self.fail_victims:
            raise RuntimeError(f"simulated failure on {victim}")
        raw_dir.mkdir(parents=True, exist_ok=True)
        (raw_dir / "raw_marker.json").write_text(json.dumps(meta), encoding="utf-8")
        return {"raw_dir": str(raw_dir)}

    def run_stats(
        self,
        raw_dir: Path,
        post_out_dir: Path,
        post_cfg: Dict[str, Any],
        report_cfg: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        meta = json.loads((raw_dir / "raw_marker.json").read_text(encoding="utf-8"))
        victim = meta["victim"]
        return [
            {
                "scenario_id": f"DL_{meta['interferer']}_vs_{victim}",
                "direction": "DL",
                "criteria_set": "SA1027-6_8025-8400",
                "pass": True,
                "max_exceed_percent": 0.001,
                "min_margin_db": 5.2,
                "worst_link": f"ES_A -> ES_{victim}",
            }
        ]


@pytest.fixture
def tmp_data_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "data_root"
    (root / "outputs").mkdir(parents=True)
    (root / "cache").mkdir(parents=True)
    (root / "raw").mkdir(parents=True)
    monkeypatch.setenv("XBAND_TEST_DATA_ROOT", str(root))
    return root


@pytest.fixture
def sample_configs(tmp_path: Path, tmp_data_root: Path) -> Dict[str, Path]:
    cfg_dir = tmp_path / "configs"
    cfg_dir.mkdir(parents=True)

    paths_yaml = cfg_dir / "paths.yaml"
    paths_yaml.write_text(
        yaml.safe_dump(
            {
                "data_root_env": "XBAND_TEST_DATA_ROOT",
                "env_file": None,
                "dirs": {"outputs": "outputs", "cache": "cache", "raw_inputs": "raw"},
                "layout": {
                    "batch_dir": "{batch_id}",
                    "pair_dir": "{idx}_{interferer}__vs__{victim}",
                    "post_dir": "{post_name}_{ts}",
                    "idx_width": 3,
                    "name_sanitize_regex": "[^A-Za-z0-9._-]+",
                    "name_sanitize_char": "_",
                    "timestamp_format": "%Y%m%d-%H%M%S",
                    "coordination_table_file": "coordination_table.csv",
                    "validation_report_file": "validation_report.md",
                    "batch_state_file": "batch_state.json",
                    "preflight_dir": "preflight",
                    "pending_antennas_csv": "pending_antennas.csv",
                    "pending_antennas_md": "pending_antennas.md",
                    "frozen_subdir": "frozen",
                    "raw_subdir": "raw",
                    "post_runs_subdir": "post_runs",
                    "pairs_subdir": "pairs",
                    "batch_summary_file": "batch_summary.xlsx",
                    "batch_verdict_file": "batch_verdict.csv",
                    "coordination_context_file": "coordination_context.json",
                    "freeze_manifest_file": "freeze_manifest.json",
                    "batch_yaml_copy_file": "batch.yaml",
                    "antenna_subdir": "antenna",
                    "libraries_subdir": "libraries",
                    "snapshots_subdir": "snapshots",
                    "input_workbook_file": "INPUT.xlsx",
                    "run_yaml_file": "run.yaml",
                    "provenance_file": "provenance.json",
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    rules_yaml = cfg_dir / "coordination_rules.yaml"
    rules_yaml.write_text(
        yaml.safe_dump(
            {
                "legacy_excel": {
                    "sheet": "List",
                    "header_row": 1,
                    "first_data_row": 2,
                    "columns": {
                        "frequency_band": "Frequency Band",
                        "operator": "Operator",
                        "satellite_network": "Satellite Network",
                        "notice_id": "Notice ID",
                        "chn_stations": "Station of CHN",
                        "reason": "受影响原因",
                    },
                },
                "user_excel": {
                    "sheet": "List",
                    "header_row": 1,
                    "first_data_row": 2,
                    "columns": {"satellite_network": "Satellite Network"},
                },
                "word": {"table_index": 0, "columns": {"satellite_network": "Satellite Network"}},
                "pdf": {"columns": {"satellite_network": "Satellite Network"}},
                "normalize": {
                    "cell_separators": ["\n", ";"],
                    "strip_suffix_patterns": [r"\s*\(\s*NGSO\s*\)\s*$", r"\s*\([^)]*°[EWew]?\s*\)\s*$"],
                    "case_insensitive": True,
                },
                "match": {"strategies": ["exact", "alnum_insensitive"], "allow_multiple_notices": False},
                "filter": {
                    "exclude_reasons": [],
                    "require_chn_stations": True,
                    "band_text_prefilter": True,
                    "band_range_regex": r"(\d+(?:\.\d+)?)\s*[-~～至]\s*(\d+(?:\.\d+)?)",
                    "required_services": ["EESS"],
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    policy_yaml = cfg_dir / "antenna_policy.yaml"
    policy_yaml.write_text(
        yaml.safe_dump(
            {
                "source_order": ["user_decision", "gims_vector", "sheet5_discrete", "gims_figure_verified", "itu_model"],
                "required_phi_max_deg": 180.0,
                "phi_step_deg": 0.5,
                "peak_tol_db": 1.0,
                "beamwidth_tol_frac": 0.25,
                "itu_model": {
                    "required_params": {
                        "REC-465-5": ["gmax_dbi"],
                        "REC-1528": ["gmax_dbi", "altitude_km"],
                        "REC-672-10DB": ["gmax_dbi", "beamlet"],
                    }
                },
                "earth_station": {
                    "model_map": {"REC-465-5": "REC-465-5"},
                    "allow_fallback": False,
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    decisions_yaml = cfg_dir / "antenna_decisions.yaml"
    decisions_yaml.write_text(yaml.safe_dump({"decisions": []}, sort_keys=False), encoding="utf-8")

    # Legacy Excel input
    xlsx_path = tmp_path / "legacy_coord.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "List"
    ws.append(["Frequency Band", "Operator", "Satellite Network", "Notice ID", "Station of CHN", "受影响原因"])
    ws.append(["8025-8400", "OrgA", "VICTIM-1 (NGSO)\nVICTIM-REV (NGSO)", None, "KASHI", "频率重叠"])
    ws.append(["8025-8400", "OrgB", "FY-2A (105°E)\nAMBIG-NET", None, None, "频率重叠"])
    wb.save(xlsx_path)

    batch_yaml = cfg_dir / "batch.yaml"
    batch_yaml.write_text(
        yaml.safe_dump(
            {
                "batch_id": "TEST_BATCH",
                "evaluated_network": "SZU-1",
                "evaluated_role": "interferer",
                "bands": [
                    {"name": "X-DL", "f_low_mhz": 8025.0, "f_high_mhz": 8400.0, "directions": ["DL"]},
                ],
                "coordination": {
                    "inputs": [{"path": str(xlsx_path), "kind": "legacy_excel"}],
                    "rules": "coordination_rules.yaml",
                    "decisions": None,
                },
                "time": {"start_utc": "2026-09-01T00:00:00Z", "duration_days": 10.0, "coarse_step_s": 60.0},
                "overrides": None,
                "antenna": {"policy": "antenna_policy.yaml", "decisions": "antenna_decisions.yaml"},
                "engine": {
                    "catalog_adapter": "xband_batch.adapters.xband_ngso:XBandNgsoCatalogAdapter",
                    "engine_adapter": "xband_batch.adapters.xband_ngso:XBandNgsoEngineAdapter",
                    "options": {"country_filter": "CHN"},
                },
                "runner": {"max_workers": 1, "resume": True, "continue_on_error": True},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    post_yaml = cfg_dir / "post.yaml"
    post_yaml.write_text(
        yaml.safe_dump(
            {
                "post_name": "test_post",
                "engine_post": {"post_yaml": "dummy.yaml", "overrides": {}},
                "report": {"enabled": False, "org": "TestOrg"},
                "summary": {
                    "sort_by": ["pass", "min_margin_db"],
                    "columns": ["pair_id", "interferer", "victim", "scenario_id", "pass", "min_margin_db"],
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    return {
        "cfg_dir": cfg_dir,
        "paths": paths_yaml,
        "rules": rules_yaml,
        "policy": policy_yaml,
        "decisions": decisions_yaml,
        "batch": batch_yaml,
        "post": post_yaml,
        "xlsx": xlsx_path,
    }


def make_network(
    name: str,
    ntc_id: int,
    direction: str = "DL",
    orbit_type: str = "NGSO",
    country: str = "CHN",
    sat_pattern_id: str = "REC-1528",
    ant_type: str = "REC-465-5",
) -> NetworkExtract:
    emi_rcp = "E" if direction == "DL" else "R"
    return NetworkExtract(
        sat_name=name,
        ntc_id=ntc_id,
        adm="CHN",
        orbit_type=orbit_type,
        planes=[
            OrbitDescriptor(
                plane_id="P1",
                n_sats=1,
                inc_deg=97.5,
                node_deg=0.0,
                node_type="LAN",
                argp_deg=0.0,
                altitude_km=530.0,
                sma_km=None,
                ecc=0.0,
                mean_anomalies_deg=[0.0],
            )
        ],
        carriers=[
            CarrierDescriptor(
                carrier_id=f"XDD-{direction}",
                beam_name="XDD",
                emi_rcp=emi_rcp,
                direction=direction,
                f_low_mhz=8100.0,
                f_high_mhz=8200.0,
                bandwidth_hz=100e6,
                power_dbw=10.0,
                psd_dbw_hz=-70.0,
                sat_gain_dbi=20.0,
                sat_pattern_id=sat_pattern_id,
                services=["EESS"],
                station_ids=["ES1"],
            )
        ],
        stations=[
            EarthStationDescriptor(
                station_id="ES1",
                direction=direction,
                beam_name="XDD",
                emi_rcp=emi_rcp,
                lon_deg=116.0,
                lat_deg=40.0,
                alt_m=50.0,
                gmax_dbi=45.0,
                beamwidth_deg=1.0,
                ant_diam_m=5.0,
                ant_type=ant_type,
                min_elev_deg=5.0,
                rx_noise_temp_k=150.0,
                country=country,
            )
        ],
    )
