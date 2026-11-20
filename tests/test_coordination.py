"""Tests for coordination list parsing, normalization, ambiguity handling, and classification."""

from __future__ import annotations

from pathlib import Path

from tests.conftest import FakeCatalogAdapter, make_network
from xband_batch.adapters.base import NetworkCandidate
from xband_batch.config import BatchConfig, PathsConfig, load_model
from xband_batch.coordination import run_parse_list
from xband_batch.paths import Layout


def test_parse_normalize_match_and_classify(sample_configs: dict[str, Path]) -> None:
    paths_cfg = load_model(PathsConfig, sample_configs["paths"])
    batch_cfg = load_model(BatchConfig, sample_configs["batch"])
    layout = Layout(paths_cfg)

    szu = make_network("SZU-1", ntc_id=1, direction="DL")
    vic_co = make_network("VICTIM-1", ntc_id=2, direction="DL")
    vic_rev = make_network("VICTIM-REV", ntc_id=3, direction="UL")

    catalog = FakeCatalogAdapter(
        candidates_by_name={
            "SZU-1": [NetworkCandidate("SZU-1", 1, "CHN", "NGSO")],
            "VICTIM-1": [NetworkCandidate("VICTIM-1", 2, "CHN", "NGSO")],
            "VICTIM-REV": [NetworkCandidate("VICTIM-REV", 3, "CHN", "NGSO")],
            "FY-2A": [NetworkCandidate("FY-2A", 4, "CHN", "GSO")],
            "AMBIG-NET": [
                NetworkCandidate("AMBIG-NET", 10, "CHN", "NGSO"),
                NetworkCandidate("AMBIG-NET", 11, "CHN", "NGSO"),
            ],
        },
        extracts_by_ntc={1: szu, 2: vic_co, 3: vic_rev},
    )

    report = run_parse_list(batch_cfg, sample_configs["cfg_dir"], layout, catalog)
    by_name = {r.normalized_name: r for r in report.rows}

    assert by_name["VICTIM-1"].category == "READY_CO_DIR"
    assert by_name["VICTIM-REV"].category == "DEFERRED_REV_DIR"
    assert by_name["FY-2A"].category == "SKIP_GSO"
    assert by_name["AMBIG-NET"].category == "NEEDS_REVIEW"
    assert "ntc=10" in by_name["AMBIG-NET"].candidate_notices


def test_band_text_prefilter_skips_out_of_band_before_catalog(sample_configs: dict[str, Path]) -> None:
    import openpyxl

    paths_cfg = load_model(PathsConfig, sample_configs["paths"])
    batch_cfg = load_model(BatchConfig, sample_configs["batch"])
    layout = Layout(paths_cfg)

    xlsx = sample_configs["cfg_dir"] / "band_filter.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "List"
    ws.append(["Frequency Band", "Operator", "Satellite Network", "Notice ID", "Station of CHN", "受影响原因"])
    ws.append(["2025-2110 (上行)", "OrgS", "SBAND-ONLY (NGSO)", None, "KASHI", "频率重叠"])
    ws.append(["8025-8400 (下行)", "OrgX", "VICTIM-1 (NGSO)", None, "KASHI", "频率重叠"])
    wb.save(xlsx)

    data = batch_cfg.model_dump()
    data["coordination"]["inputs"] = [{"path": str(xlsx), "kind": "legacy_excel"}]
    batch_cfg = BatchConfig.model_validate(data)

    szu = make_network("SZU-1", ntc_id=1, direction="DL")
    vic = make_network("VICTIM-1", ntc_id=2, direction="DL")
    catalog = FakeCatalogAdapter(
        candidates_by_name={
            "SZU-1": [NetworkCandidate("SZU-1", 1, "CHN", "NGSO")],
            "VICTIM-1": [NetworkCandidate("VICTIM-1", 2, "CHN", "NGSO")],
        },
        extracts_by_ntc={1: szu, 2: vic},
    )
    report = run_parse_list(batch_cfg, sample_configs["cfg_dir"], layout, catalog)
    by_name = {r.normalized_name: r for r in report.rows}
    assert by_name["SBAND-ONLY"].category == "SKIP_FILTERED"
    assert by_name["VICTIM-1"].category == "READY_CO_DIR"


def test_allow_multiple_notices_prefers_notice_with_associated_stations(
    sample_configs: dict[str, Path],
) -> None:
    import openpyxl
    import yaml

    paths_cfg = load_model(PathsConfig, sample_configs["paths"])
    batch_cfg = load_model(BatchConfig, sample_configs["batch"])
    layout = Layout(paths_cfg)

    rules_path = sample_configs["cfg_dir"] / "coordination_rules.yaml"
    rules_data = yaml.safe_load(rules_path.read_text(encoding="utf-8"))
    rules_data["match"]["allow_multiple_notices"] = True
    rules_data["match"]["prefer_associated_earth_stations"] = True
    rules_path.write_text(yaml.safe_dump(rules_data, sort_keys=False), encoding="utf-8")

    xlsx = sample_configs["cfg_dir"] / "multi_notice.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "List"
    ws.append(["Frequency Band", "Operator", "Satellite Network", "Notice ID", "Station of CHN", "受影响原因"])
    ws.append(["8025-8400", "Org", "JT2-LIKE\nFOUR-NOTICE\nTYPICAL-ONLY", None, "", "频率重叠"])
    wb.save(xlsx)

    data = batch_cfg.model_dump()
    data["coordination"]["inputs"] = [{"path": str(xlsx), "kind": "legacy_excel"}]
    batch_cfg = BatchConfig.model_validate(data)

    szu = make_network("SZU-1", ntc_id=1, direction="DL")
    # JT2-LIKE: ntc=102 is newer (2024-07-25) with 0 target-band stations; ntc=101 (2024-07-24) has stations
    jt2_old_with_stn = make_network("JT2-LIKE", ntc_id=101, direction="DL")
    jt2_new_empty = make_network("JT2-LIKE", ntc_id=102, direction="DL", country="USA")
    jt2_new_empty.carriers.clear()
    jt2_new_empty.stations.clear()

    # FOUR-NOTICE: ntc=201 (2020) has 1 CHN station, 202/203 empty, 204 (2024) has CHN stations -> picks 204
    fn1 = make_network("FOUR-NOTICE", ntc_id=201, direction="DL")
    fn2 = make_network("FOUR-NOTICE", ntc_id=202, direction="DL", country="USA")
    fn3 = make_network("FOUR-NOTICE", ntc_id=203, direction="DL", country="USA")
    fn4 = make_network("FOUR-NOTICE", ntc_id=204, direction="DL")

    # TYPICAL-ONLY: 0 CHN specific stations in all notices -> stays SKIP_NO_CHN_ES
    typ = make_network("TYPICAL-ONLY", ntc_id=301, direction="DL", country="USA")
    typ.stations.clear()
    typ.carriers[0].station_ids.clear()

    catalog = FakeCatalogAdapter(
        candidates_by_name={
            "SZU-1": [NetworkCandidate("SZU-1", 1, "CHN", "NGSO")],
            "JT2-LIKE": [
                NetworkCandidate("JT2-LIKE", 102, "CHN", "NGSO", d_rcv="2024-07-25"),
                NetworkCandidate("JT2-LIKE", 101, "CHN", "NGSO", d_rcv="2024-07-24"),
            ],
            "FOUR-NOTICE": [
                NetworkCandidate("FOUR-NOTICE", 204, "CHN", "NGSO", d_rcv="2024-05-01"),
                NetworkCandidate("FOUR-NOTICE", 203, "CHN", "NGSO", d_rcv="2023-05-01"),
                NetworkCandidate("FOUR-NOTICE", 202, "CHN", "NGSO", d_rcv="2022-05-01"),
                NetworkCandidate("FOUR-NOTICE", 201, "CHN", "NGSO", d_rcv="2020-05-01"),
            ],
            "TYPICAL-ONLY": [NetworkCandidate("TYPICAL-ONLY", 301, "CHN", "NGSO", d_rcv="2024-01-01")],
        },
        extracts_by_ntc={
            1: szu,
            101: jt2_old_with_stn,
            102: jt2_new_empty,
            201: fn1,
            202: fn2,
            203: fn3,
            204: fn4,
            301: typ,
        },
    )

    report = run_parse_list(batch_cfg, sample_configs["cfg_dir"], layout, catalog)
    by_name = {r.normalized_name: r for r in report.rows}

    assert by_name["JT2-LIKE"].category == "READY_CO_DIR"
    assert by_name["JT2-LIKE"].ntc_id == 101
    assert by_name["FOUR-NOTICE"].category == "READY_CO_DIR"
    assert by_name["FOUR-NOTICE"].ntc_id == 204
    assert by_name["TYPICAL-ONLY"].category == "SKIP_NO_CHN_ES"


def test_merge_sibling_notice_stations_unions_and_deduplicates(monkeypatch) -> None:
    import sys
    from types import SimpleNamespace
    from xband_batch.adapters.xband_ngso import _merge_sibling_notice_stations

    net = make_network("MULTI", ntc_id=4, direction="DL")
    net.adm = "CHN"
    # Primary notice 4 has ES1 at (116.0, 40.0) on beam XDD
    assert net.carriers[0].station_ids == ["ES1"]

    def fake_query(_conn, sql, _params):
        if "from public.non_geo g join public.notice" in sql:
            return [{"ntc_id": 1}]
        # Sibling notice 1 returns ES1_DUP (within 0.01 deg of ES1) and ES_EXTRA (distinct city)
        return [
            {
                "ntc_id": 1, "grp_id": 10, "beam_name": "XDD", "emi_rcp": "E",
                "freq_min": 8025.0, "freq_max": 8400.0, "grp_noise_t": 150.0,
                "seq_no": 1, "stn_name": "ES1_DUP", "long_dec": 116.002, "lat_dec": 40.001,
                "ant_alt": 100.0, "gain": 45.0, "bmwdth": 1.0, "ant_diam": 4.5,
                "noise_t": 150.0, "ctry": "CHN", "ant_type": "REC-465-5",
            },
            {
                "ntc_id": 1, "grp_id": 10, "beam_name": "XDD", "emi_rcp": "E",
                "freq_min": 8025.0, "freq_max": 8400.0, "grp_noise_t": 150.0,
                "seq_no": 2, "stn_name": "ES_EXTRA", "long_dec": 75.93, "lat_dec": 39.5,
                "ant_alt": 50.0, "gain": 46.0, "bmwdth": 1.0, "ant_diam": 4.5,
                "noise_t": 150.0, "ctry": "CHN", "ant_type": "REC-465-5",
            },
        ]

    monkeypatch.setitem(sys.modules, "x_band_ngso.itu.db", SimpleNamespace(query=fake_query))
    itu_cfg = SimpleNamespace(
        earth_station=SimpleNamespace(
            position_tolerance_deg=0.01,
            default_gain_dbi=None,
            default_alt_m=0.0,
            default_min_elev_deg=5.0,
        )
    )
    merged = _merge_sibling_notice_stations(None, net, "CHN", itu_cfg)
    assert merged.carriers[0].station_ids == ["ES1", "ES_EXTRA"]
    assert [s.station_id for s in merged.stations] == ["ES1", "ES_EXTRA"]
    assert merged.metadata["merged_station_notices"] == [1]
