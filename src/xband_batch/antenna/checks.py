"""Antenna curve integrity & consistency checks driven by antenna_policy.yaml."""

from __future__ import annotations

import math
from typing import List, Optional

import numpy as np

from ..config.schemas import AntennaPolicyConfig
from .models import AntennaCurve, AntennaIssue, AntennaRequest


def _estimate_3db_beamwidth(phi: np.ndarray, gain: np.ndarray) -> Optional[float]:
    if len(phi) < 2:
        return None
    target = float(gain[0]) - 3.0
    below = np.flatnonzero(gain <= target)
    if len(below) == 0:
        return None
    idx = int(below[0])
    if idx == 0:
        return 0.0
    g0, g1 = float(gain[idx - 1]), float(gain[idx])
    p0, p1 = float(phi[idx - 1]), float(phi[idx])
    if g1 == g0:
        return 2.0 * p1
    frac = (target - g0) / (g1 - g0)
    half_bw = p0 + frac * (p1 - p0)
    return 2.0 * half_bw


def run_checks(
    curve: AntennaCurve,
    req: AntennaRequest,
    policy: AntennaPolicyConfig,
) -> List[AntennaIssue]:
    issues: List[AntennaIssue] = []
    phi = np.asarray(curve.phi_deg, dtype=float)
    gain = np.asarray(curve.gain_dbi, dtype=float)

    if len(phi) < 2 or len(phi) != len(gain):
        issues.append(AntennaIssue("NON_FINITE", "curve must contain at least 2 matching (phi, gain) points"))
        return issues

    if not (np.isfinite(phi).all() and np.isfinite(gain).all()):
        issues.append(AntennaIssue("NON_FINITE", "curve contains NaN or Inf values"))
        return issues

    if np.any(np.diff(phi) <= 0):
        issues.append(AntennaIssue("NOT_MONOTONIC", "phi_deg is not strictly increasing"))

    if float(phi[-1]) < policy.required_phi_max_deg:
        issues.append(
            AntennaIssue(
                "PHI_RANGE_SHORT",
                f"max phi is {float(phi[-1]):.2f} deg < required {policy.required_phi_max_deg:.2f} deg",
            )
        )

    is_model = bool(curve.metadata.get("model_id") or curve.metadata.get("model"))
    if is_model:
        return issues  # a standard model is its own definition; peak/beamwidth checks do not apply

    if req.declared_gmax_dbi is not None:
        curve_peak = float(np.max(gain))
        diff = abs(curve_peak - req.declared_gmax_dbi)
        if diff > policy.peak_tol_db:
            issues.append(
                AntennaIssue(
                    "PEAK_MISMATCH",
                    f"curve peak {curve_peak:.2f} dBi differs from declared {req.declared_gmax_dbi:.2f} dBi "
                    f"by {diff:.2f} dB (> tol {policy.peak_tol_db:.2f} dB)",
                )
            )

    if req.declared_beamwidth_deg is not None and req.declared_beamwidth_deg > 0:
        est_bw = _estimate_3db_beamwidth(phi, gain)
        if est_bw is not None:
            rel_err = abs(est_bw - req.declared_beamwidth_deg) / req.declared_beamwidth_deg
            if rel_err > policy.beamwidth_tol_frac:
                issues.append(
                    AntennaIssue(
                        "BEAMWIDTH_MISMATCH",
                        f"estimated -3 dB beamwidth {est_bw:.2f} deg differs from declared "
                        f"{req.declared_beamwidth_deg:.2f} deg by {rel_err * 100:.1f}%",
                    )
                )

    return issues
