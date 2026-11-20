"""Pluggable curve transforms: ONLY executed when explicitly approved in antenna_decisions.yaml."""

from __future__ import annotations

from typing import Any, Callable, Dict

import numpy as np

from ..config.schemas import AntennaPolicyConfig
from ..errors import ConfigError
from .models import AntennaCurve, AntennaRequest, DecisionNotApplicable

TransformFn = Callable[[AntennaCurve, AntennaRequest, AntennaPolicyConfig, Dict[str, Any]], AntennaCurve]
_TRANSFORMS: Dict[str, TransformFn] = {}


def register_transform(name: str):
    def _wrap(fn: TransformFn) -> TransformFn:
        _TRANSFORMS[name] = fn
        return fn

    return _wrap


def apply_transform(
    name: str,
    curve: AntennaCurve,
    req: AntennaRequest,
    policy: AntennaPolicyConfig,
    params: Dict[str, Any],
) -> AntennaCurve:
    fn = _TRANSFORMS.get(name)
    if fn is None:
        raise ConfigError(
            "antenna_decisions",
            "action",
            f"unknown transform action {name!r}; registered: {sorted(_TRANSFORMS)}",
        )
    return fn(curve, req, policy, params)


@register_transform("trust_curve")
def _trust_curve(
    curve: AntennaCurve,
    req: AntennaRequest,
    policy: AntennaPolicyConfig,
    params: Dict[str, Any],
) -> AntennaCurve:
    return AntennaCurve(
        phi_deg=list(curve.phi_deg),
        gain_dbi=list(curve.gain_dbi),
        source_name=curve.source_name,
        metadata={**curve.metadata, "transform": "trust_curve"},
    )


@register_transform("shift_to_declared_peak")
def _shift_to_declared_peak(
    curve: AntennaCurve,
    req: AntennaRequest,
    policy: AntennaPolicyConfig,
    params: Dict[str, Any],
) -> AntennaCurve:
    if req.declared_gmax_dbi is None:
        raise ConfigError("antenna", "declared_gmax_dbi", "cannot shift peak when declared_gmax_dbi is null")
    gain = np.asarray(curve.gain_dbi, dtype=float)
    delta = float(req.declared_gmax_dbi) - float(np.max(gain))
    shifted = (gain + delta).tolist()
    return AntennaCurve(
        phi_deg=list(curve.phi_deg),
        gain_dbi=shifted,
        source_name=curve.source_name,
        metadata={**curve.metadata, "transform": "shift_to_declared_peak", "shift_db": delta},
    )


def _check_gap(curve: AntennaCurve, policy: AntennaPolicyConfig, params: Dict[str, Any]) -> None:
    """Optional guard: the approval only covers tails shorter than params['max_gap_deg']."""
    limit = params.get("max_gap_deg")
    if limit is None:
        return
    gap = policy.required_phi_max_deg - float(curve.phi_deg[-1])
    if gap > float(limit) + 1e-9:
        raise DecisionNotApplicable(
            f"missing tail {gap:.2f} deg exceeds the approved max_gap_deg {float(limit):.2f} deg"
        )


@register_transform("hold_last")
def _hold_last(
    curve: AntennaCurve,
    req: AntennaRequest,
    policy: AntennaPolicyConfig,
    params: Dict[str, Any],
) -> AntennaCurve:
    _check_gap(curve, policy, params)
    phi = list(curve.phi_deg)
    gain = list(curve.gain_dbi)
    if phi[-1] < policy.required_phi_max_deg:
        next_phi = phi[-1] + policy.phi_step_deg
        last_g = gain[-1]
        while next_phi <= policy.required_phi_max_deg + 1e-9:
            phi.append(round(next_phi, 6))
            gain.append(last_g)
            next_phi += policy.phi_step_deg
    return AntennaCurve(
        phi_deg=phi,
        gain_dbi=gain,
        source_name=curve.source_name,
        metadata={**curve.metadata, "transform": "hold_last"},
    )


@register_transform("constant_floor")
def _constant_floor(
    curve: AntennaCurve,
    req: AntennaRequest,
    policy: AntennaPolicyConfig,
    params: Dict[str, Any],
) -> AntennaCurve:
    if "value_dbi" not in params:
        raise ConfigError("antenna_decisions", "params.value_dbi", "constant_floor requires value_dbi")
    floor_val = float(params["value_dbi"])
    _check_gap(curve, policy, params)
    phi = list(curve.phi_deg)
    gain = list(curve.gain_dbi)
    if phi[-1] < policy.required_phi_max_deg:
        next_phi = phi[-1] + policy.phi_step_deg
        while next_phi <= policy.required_phi_max_deg + 1e-9:
            phi.append(round(next_phi, 6))
            gain.append(floor_val)
            next_phi += policy.phi_step_deg
    return AntennaCurve(
        phi_deg=phi,
        gain_dbi=gain,
        source_name=curve.source_name,
        metadata={**curve.metadata, "transform": "constant_floor", "value_dbi": floor_val},
    )
