"""Tests for AntennaResolver: strict priority, zero silent fallback, explicit decisions."""

from __future__ import annotations

from pathlib import Path

from xband_batch.antenna import AntennaRequest, AntennaResolver
from xband_batch.antenna.models import AmbiguousPatternError
from xband_batch.config import AntennaPolicyConfig, load_model
from xband_batch.config.schemas import AntennaDecisionEntry, DecisionKey


def _full_curve():
    phi = [0.0, 90.0, 180.0]
    gain = [20.0, 0.0, -10.0]
    return phi, gain


def test_no_silent_fallback_on_missing_params(sample_configs: dict[str, Path]) -> None:
    policy = load_model(AntennaPolicyConfig, sample_configs["policy"])
    resolver = AntennaResolver(
        policy,
        decisions=[],
        source_context={
            # Even if a model_evaluator exists, missing beamlet on REC-672-10DB must block without guessing
            "model_evaluator": lambda *_: _full_curve(),
        },
    )
    req = AntennaRequest(
        network="TEST-SAT",
        ntc_id=100,
        beam="B1",
        emi_rcp="E",
        role="space",
        declared_gmax_dbi=20.0,
        model_name="REC-672-10DB",
        model_params={},  # beamlet missing!
    )
    res = resolver.resolve(req)
    assert res.status == "pending"
    assert any(i.code == "MISSING_PARAMS" for i in res.issues_detected)


def test_earth_station_unmapped_model_blocked(sample_configs: dict[str, Path]) -> None:
    policy = load_model(AntennaPolicyConfig, sample_configs["policy"])
    resolver = AntennaResolver(policy)
    req = AntennaRequest(
        network="TEST-SAT",
        ntc_id=100,
        beam="B1",
        emi_rcp="E",
        role="earth",
        declared_gmax_dbi=40.0,
        model_name="REC-465-6-E",  # Not in sample model_map -> must NOT silently become REC-465-5
    )
    res = resolver.resolve(req)
    assert res.status == "pending"
    assert any(i.code == "UNMAPPED_MODEL" for i in res.issues_detected)


def test_short_phi_range_requires_explicit_decision(sample_configs: dict[str, Path]) -> None:
    policy = load_model(AntennaPolicyConfig, sample_configs["policy"])
    short_curve = ([0.0, 15.0, 30.0], [20.0, 10.0, -5.0])

    # Without decision -> pending
    resolver_no_dec = AntennaResolver(
        policy,
        decisions=[],
        source_context={"gims_figure_provider": lambda _req: short_curve},
    )
    req = AntennaRequest(
        network="2M8M-SAT",
        ntc_id=101,
        beam="XDD",
        emi_rcp="E",
        role="space",
        declared_gmax_dbi=20.0,
    )
    blocked = resolver_no_dec.resolve(req)
    assert blocked.status == "pending"
    assert [i.code for i in blocked.issues_detected] == ["PHI_RANGE_SHORT"]

    # With explicit hold_last decision -> resolved cleanly up to 180 deg
    dec = AntennaDecisionEntry(
        id="DEC-1",
        key=DecisionKey(network="2M8M-SAT", beam="XDD", emi_rcp="E", role="space"),
        issue="PHI_RANGE_SHORT",
        action="hold_last",
        params={},
        by="jack",
        date="2026-11-16",
        reason="approved",
    )
    resolver_with_dec = AntennaResolver(
        policy,
        decisions=[dec],
        source_context={"gims_figure_provider": lambda _req: short_curve},
    )
    ok = resolver_with_dec.resolve(req)
    assert ok.status == "resolved"
    assert ok.curve is not None
    assert ok.curve.phi_deg[-1] == 180.0
    assert ok.curve.gain_dbi[-1] == -5.0
    assert ok.applied_decisions == [("DEC-1", "hold_last")]


def test_ambiguous_source_and_max_gap_guard(sample_configs: dict[str, Path]) -> None:
    policy = load_model(AntennaPolicyConfig, sample_configs["policy"])

    def _ambig(_req):
        raise AmbiguousPatternError("two distinct curves found")

    resolver_ambig = AntennaResolver(policy, decisions=[], source_context={"gims_vector_provider": _ambig})
    req = AntennaRequest(network="SAT-A", ntc_id=1, beam="B1", emi_rcp="E", role="space", declared_gmax_dbi=20.0)
    res_ambig = resolver_ambig.resolve(req)
    assert res_ambig.status == "pending"
    assert [i.code for i in res_ambig.issues_detected] == ["AMBIGUOUS_SOURCE"]

    # Decision with max_gap_deg=5 deg must NOT apply when tail gap is 150 deg
    short_curve = ([0.0, 15.0, 30.0], [20.0, 10.0, -5.0])
    dec = AntennaDecisionEntry(
        id="DEC-GUARD",
        key=DecisionKey(network="*", beam="*", emi_rcp="*", role="space"),
        issue="PHI_RANGE_SHORT",
        action="hold_last",
        params={"max_gap_deg": 5.0},
        by="jack",
        date="2026-11-17",
        reason="only small tail allowed",
    )
    resolver_guard = AntennaResolver(
        policy,
        decisions=[dec],
        source_context={"gims_figure_provider": lambda _req: short_curve},
    )
    res_guard = resolver_guard.resolve(req)
    assert res_guard.status == "pending"
    assert "max_gap_deg" in res_guard.issues_detected[0].detail


def test_wildcard_hold_last_120_deg_threshold(sample_configs: dict[str, Path]) -> None:
    policy = load_model(AntennaPolicyConfig, sample_configs["policy"])
    dec = AntennaDecisionEntry(
        id="DEC-TAIL-120",
        key=DecisionKey(network="*", beam="*", emi_rcp="*", role="*"),
        issue="PHI_RANGE_SHORT",
        action="hold_last",
        params={"max_gap_deg": 60.0},
        by="jack",
        date="2026-11-19",
        reason="approve tail >= 120 deg",
    )
    req = AntennaRequest(network="SAT-X", ntc_id=2, beam="XDD", emi_rcp="E", role="space", declared_gmax_dbi=20.0)

    # 120.0 deg (gap == 60.0 deg) -> resolved to 180.0
    res_ok = AntennaResolver(
        policy,
        decisions=[dec],
        source_context={"gims_figure_provider": lambda _req: ([0.0, 60.0, 120.0], [20.0, 5.0, -8.0])},
    ).resolve(req)
    assert res_ok.status == "resolved"
    assert res_ok.curve is not None and res_ok.curve.phi_deg[-1] == 180.0 and res_ok.curve.gain_dbi[-1] == -8.0

    # 119.5 deg (gap == 60.5 deg > 60.0 deg) -> still pending
    res_block = AntennaResolver(
        policy,
        decisions=[dec],
        source_context={"gims_figure_provider": lambda _req: ([0.0, 60.0, 119.5], [20.0, 5.0, -8.0])},
    ).resolve(req)
    assert res_block.status == "pending"
    assert "max_gap_deg" in res_block.issues_detected[0].detail


def test_wildcard_shift_to_declared_peak(sample_configs: dict[str, Path]) -> None:
    policy = load_model(AntennaPolicyConfig, sample_configs["policy"])
    dec = AntennaDecisionEntry(
        id="DEC-PEAK-SHIFT",
        key=DecisionKey(network="*", beam="*", emi_rcp="*", role="*"),
        issue="PEAK_MISMATCH",
        action="shift_to_declared_peak",
        params={},
        by="jack",
        date="2026-11-19",
        reason="shift curve by declared - curve peak",
    )
    req = AntennaRequest(network="STARERA-3", ntc_id=1, beam="XTU1", emi_rcp="R", role="space", declared_gmax_dbi=5.9)
    res = AntennaResolver(
        policy,
        decisions=[dec],
        source_context={"gims_figure_provider": lambda _req: ([0.0, 90.0, 180.0], [3.97, -1.03, -6.03])},
    ).resolve(req)
    assert res.status == "resolved"
    assert res.curve is not None
    assert [round(g, 4) for g in res.curve.gain_dbi] == [5.9, 0.9, -4.1]
    assert res.applied_decisions == [("DEC-PEAK-SHIFT", "shift_to_declared_peak")]
