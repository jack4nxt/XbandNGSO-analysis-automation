"""Built-in antenna sources.

Sources never estimate. They either return a curve that exists in a data source, evaluate a
standard model whose required parameters are all present, or report why they cannot.
Data access is injected through `context` providers so this module stays backend-independent:

  gims_vector_provider(req)   -> (phi, gain) | None      (may raise AmbiguousPatternError)
  sheet5_provider(req)        -> (phi, gain) | None
  gims_figure_provider(req)   -> (phi, gain) | None
  model_evaluator(model_id, req, policy, params) -> (phi, gain)  (may raise KeyError/ValueError)
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Callable, List, Tuple

from ...config.schemas import AntennaPolicyConfig
from ..decisions import find_matching_decisions
from ..models import AmbiguousPatternError, AntennaCurve, AntennaIssue, AntennaRequest, SourceFetchResult
from .base import AntennaSource, register_source


def _read_pattern_csv(path: Path) -> Tuple[List[float], List[float]]:
    phi: List[float] = []
    gain: List[float] = []
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            phi.append(float(row["phi_deg"]))
            gain.append(float(row["gain_dbi"]))
    return phi, gain


def _evaluate(evaluator: Callable, model_id: str, req: AntennaRequest, policy: AntennaPolicyConfig, params: dict):
    """Run the model evaluator; return (phi, gain) or a SourceFetchResult describing the failure."""
    try:
        return evaluator(model_id, req, policy, params)
    except KeyError as exc:
        return SourceFetchResult(
            status="error",
            issues=[AntennaIssue("UNMAPPED_MODEL", f"model {model_id!r} is not implemented by the evaluator ({exc})")],
        )
    except ValueError as exc:
        return SourceFetchResult(
            status="error",
            issues=[AntennaIssue("MISSING_PARAMS", f"model {model_id!r} cannot be evaluated: {exc}")],
        )


def _provider_fetch(context: dict, provider_key: str, source_name: str, req: AntennaRequest) -> SourceFetchResult:
    provider = context.get(provider_key)
    if provider is None:
        return SourceFetchResult(status="absent", note=f"{provider_key} not wired")
    try:
        data = provider(req)
    except AmbiguousPatternError as exc:
        return SourceFetchResult(status="error", issues=[AntennaIssue("AMBIGUOUS_SOURCE", str(exc))])
    if data is None:
        return SourceFetchResult(status="absent")
    phi, gain = data
    return SourceFetchResult(
        status="found",
        curve=AntennaCurve(phi_deg=list(phi), gain_dbi=list(gain), source_name=source_name),
    )


@register_source("user_decision")
class UserDecisionSource(AntennaSource):
    """Curves explicitly approved in antenna_decisions.yaml (use_csv / use_model)."""

    def fetch(self, req: AntennaRequest, policy: AntennaPolicyConfig) -> SourceFetchResult:
        decisions = self.context.get("decisions", [])
        for dec in find_matching_decisions(decisions, req, issue_code=None):
            if dec.action == "use_csv":
                csv_path = Path(dec.params["csv_path"]).expanduser()
                if not csv_path.is_file():
                    return SourceFetchResult(
                        status="error",
                        issues=[AntennaIssue("NO_SOURCE", f"decision {dec.id} points to missing CSV {csv_path}")],
                    )
                phi, gain = _read_pattern_csv(csv_path)
                return SourceFetchResult(
                    status="found",
                    curve=AntennaCurve(
                        phi_deg=phi,
                        gain_dbi=gain,
                        source_name="user_decision",
                        metadata={"decision_id": dec.id, "action": dec.action, "csv_path": str(csv_path)},
                    ),
                )
            if dec.action == "use_model":
                model_id = dec.params.get("model")
                if not model_id:
                    return SourceFetchResult(
                        status="error",
                        issues=[AntennaIssue("MISSING_PARAMS", f"decision {dec.id} missing params.model")],
                    )
                evaluator = self.context.get("model_evaluator")
                if evaluator is None:
                    return SourceFetchResult(
                        status="error",
                        issues=[AntennaIssue("MISSING_PARAMS", f"decision {dec.id}: model evaluator not wired")],
                    )
                params = dict(req.model_params)
                params.update(dec.params.get("params", {}))
                if req.declared_gmax_dbi is not None:
                    params.setdefault("gmax_dbi", req.declared_gmax_dbi)
                if req.altitude_km is not None:
                    params.setdefault("altitude_km", req.altitude_km)
                out = _evaluate(evaluator, model_id, req, policy, params)
                if isinstance(out, SourceFetchResult):
                    return out
                phi, gain = out
                return SourceFetchResult(
                    status="found",
                    curve=AntennaCurve(
                        phi_deg=list(phi),
                        gain_dbi=list(gain),
                        source_name="user_decision",
                        metadata={"decision_id": dec.id, "action": dec.action, "model": model_id},
                    ),
                )
        return SourceFetchResult(status="absent")


@register_source("gims_vector")
class GimsVectorSource(AntennaSource):
    """ITU GIMS native vector curves."""

    def fetch(self, req: AntennaRequest, policy: AntennaPolicyConfig) -> SourceFetchResult:
        return _provider_fetch(self.context, "gims_vector_provider", "gims_vector", req)


@register_source("sheet5_discrete")
class Sheet5DiscreteSource(AntennaSource):
    """Discrete phi-gain points from the network parameter workbook (sheet5)."""

    def fetch(self, req: AntennaRequest, policy: AntennaPolicyConfig) -> SourceFetchResult:
        return _provider_fetch(self.context, "sheet5_provider", "sheet5_discrete", req)


@register_source("gims_figure_verified")
class GimsFigureVerifiedSource(AntennaSource):
    """Pre-verified digitized GIMS raster curves (read from the figure's own axes)."""

    def fetch(self, req: AntennaRequest, policy: AntennaPolicyConfig) -> SourceFetchResult:
        return _provider_fetch(self.context, "gims_figure_provider", "gims_figure_verified", req)


@register_source("itu_model")
class ItuModelSource(AntennaSource):
    """Standard ITU-R model evaluated with explicit, complete parameters (no estimation)."""

    def fetch(self, req: AntennaRequest, policy: AntennaPolicyConfig) -> SourceFetchResult:
        raw_model = req.model_name
        if not raw_model:
            return SourceFetchResult(status="absent")

        if req.role == "earth":
            mapped = policy.earth_station.model_map.get(raw_model)
            if mapped is None:
                return SourceFetchResult(
                    status="error",
                    issues=[
                        AntennaIssue(
                            "UNMAPPED_MODEL",
                            f"earth-station antenna type {raw_model!r} is not in earth_station.model_map "
                            "(fallback is disabled)",
                        )
                    ],
                )
            model_id = mapped
        else:
            model_id = raw_model

        required = policy.itu_model.required_params.get(model_id)
        if required is None:
            return SourceFetchResult(
                status="error",
                issues=[AntennaIssue("UNMAPPED_MODEL", f"model {model_id!r} is not declared in itu_model.required_params")],
            )

        available = dict(req.model_params)
        if req.declared_gmax_dbi is not None:
            available.setdefault("gmax_dbi", req.declared_gmax_dbi)
        if req.altitude_km is not None:
            available.setdefault("altitude_km", req.altitude_km)

        missing = [p for p in required if available.get(p) is None]
        if missing:
            return SourceFetchResult(
                status="error",
                issues=[
                    AntennaIssue(
                        "MISSING_PARAMS",
                        f"model {model_id!r} missing required parameter(s) {missing}; "
                        "estimation from Gmax is forbidden",
                    )
                ],
            )

        evaluator = self.context.get("model_evaluator")
        if evaluator is None:
            return SourceFetchResult(
                status="error",
                issues=[AntennaIssue("MISSING_PARAMS", f"model {model_id!r}: model evaluator not wired")],
            )

        out = _evaluate(evaluator, model_id, req, policy, available)
        if isinstance(out, SourceFetchResult):
            return out
        phi, gain = out
        return SourceFetchResult(
            status="found",
            curve=AntennaCurve(
                phi_deg=list(phi),
                gain_dbi=list(gain),
                source_name="itu_model",
                metadata={"model_id": model_id},
            ),
        )
