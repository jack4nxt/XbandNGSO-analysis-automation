"""Tests for user parameter overrides and curve_policy enforcement."""

from __future__ import annotations

import pytest

from tests.conftest import make_network
from xband_batch.antenna import AntennaCurve
from xband_batch.config.schemas import OverrideItem, OverridesConfig, OverrideSelect, OverrideTarget
from xband_batch.overrides import apply_overrides


def test_gmax_override_requires_curve_policy() -> None:
    with pytest.raises(ValueError):
        OverrideItem(
            id="BAD",
            target=OverrideTarget(network="SZU-1", role="interferer"),
            select=OverrideSelect(kind="carrier", beam="XDD", direction="DL"),
            set={},
            delta={"sat_gmax_dbi": -3.0},
            curve_policy=None,
            reason="missing curve_policy",
        )


def test_set_and_delta_apply_with_psd_pairing_and_curve_shift() -> None:
    net = make_network("SZU-1", ntc_id=1, direction="DL")
    key = ("SZU-1", "XDD", "E", "space", "")
    curves = {
        key: AntennaCurve([0.0, 180.0], [20.0, -10.0], "gims_vector")
    }
    cfg = OverridesConfig(
        psd_consistency_mode="recompute_missing_or_paired",
        items=[
            OverrideItem(
                id="OVR-1",
                target=OverrideTarget(network="SZU-1", role="interferer"),
                select=OverrideSelect(kind="carrier", beam="XDD", direction="DL"),
                set={"psd_dbw_hz": -60.0},
                delta={"sat_gmax_dbi": -3.0},
                curve_policy="shift",
                reason="test override",
            )
        ],
    )
    out_net, out_curves, records = apply_overrides(net, "interferer", cfg, curves)
    c = out_net.carriers[0]
    assert c.psd_dbw_hz == -60.0
    assert c.power_dbw == pytest.approx(20.0)  # -60 + 10*log10(100e6) = +20 dBW
    assert c.sat_gain_dbi == 17.0
    assert out_curves[key].gain_dbi == [17.0, -13.0]
    assert len(records) >= 3
