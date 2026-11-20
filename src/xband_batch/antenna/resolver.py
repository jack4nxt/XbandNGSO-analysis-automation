"""AntennaResolver: strict priority chain, zero silent fallback, explicit decision gate."""

from __future__ import annotations

from typing import Any, Dict, List

from ..config.schemas import AntennaDecisionEntry, AntennaPolicyConfig
from .checks import run_checks
from .decisions import find_matching_decisions
from .models import (
    AmbiguousPatternError,
    AntennaIssue,
    DecisionNotApplicable,
    AntennaRequest,
    PendingAntennaItem,
    ResolvedAntenna,
)
from .sources import create_source
from .transforms import apply_transform


class AntennaResolver:
    """Resolves a single AntennaRequest strictly by policy without silent calculation."""

    def __init__(
        self,
        policy: AntennaPolicyConfig,
        decisions: List[AntennaDecisionEntry] | None = None,
        source_context: Dict[str, Any] | None = None,
    ):
        self.policy = policy
        self.decisions = decisions or []
        ctx = dict(source_context or {})
        ctx["decisions"] = self.decisions
        self.sources = [(name, create_source(name, ctx)) for name in policy.source_order]

    def _pending(
        self,
        req: AntennaRequest,
        attempted: List[str],
        issues: List[AntennaIssue],
    ) -> ResolvedAntenna:
        return ResolvedAntenna(
            request=req,
            status="pending",
            issues_detected=issues,
            pending_item=PendingAntennaItem(
                network=req.network,
                ntc_id=req.ntc_id,
                beam=req.beam,
                emi_rcp=req.emi_rcp,
                role=req.role,
                declared_gmax_dbi=req.declared_gmax_dbi,
                altitude_km=req.altitude_km,
                attempted_sources=attempted,
                issues=issues,
                raw_image_path=req.raw_image_path,
                variant=req.variant,
            ),
        )

    def resolve(self, req: AntennaRequest) -> ResolvedAntenna:
        # 1. Check explicit exclusion first
        for dec in find_matching_decisions(self.decisions, req, issue_code=None):
            if dec.action == "exclude":
                return ResolvedAntenna(
                    request=req,
                    status="excluded",
                    applied_decisions=[(dec.id, dec.action)],
                )

        # 2. Walk configured sources strictly in priority order
        attempted: List[str] = []
        chosen_curve = None
        for src_name, src in self.sources:
            attempted.append(src_name)
            res = src.fetch(req, self.policy)
            if res.status == "absent":
                continue
            if res.status == "error":
                # Do NOT fall through to lower-priority sources or estimate!
                return self._pending(req, attempted, res.issues)
            if res.status == "found" and res.curve is not None:
                chosen_curve = res.curve
                break

        if chosen_curve is None:
            return self._pending(
                req,
                attempted,
                [AntennaIssue("NO_SOURCE", "no antenna source yielded a pattern; explicit decision required")],
            )

        # 3. Validate candidate curve against policy thresholds
        issues = run_checks(chosen_curve, req, self.policy)
        if not issues:
            return ResolvedAntenna(
                request=req,
                status="resolved",
                curve=chosen_curve,
                source_used=chosen_curve.source_name,
            )

        # 4. For every detected issue, require an explicit matching decision (no implicit transforms)
        applied_decisions = []
        current_curve = chosen_curve
        unresolved: List[AntennaIssue] = []

        for issue in issues:
            decs = [
                d
                for d in find_matching_decisions(self.decisions, req, issue_code=issue.code)
                if d.action not in ("use_csv", "use_model")
            ]
            if not decs:
                unresolved.append(issue)
                continue
            dec = decs[0]
            try:
                current_curve = apply_transform(dec.action, current_curve, req, self.policy, dec.params)
            except DecisionNotApplicable as exc:
                unresolved.append(AntennaIssue(issue.code, f"{issue.detail} [decision {dec.id} not applicable: {exc}]"))
                continue
            applied_decisions.append((dec.id, dec.action))

        if unresolved:
            return self._pending(req, attempted, unresolved)

        # Re-check structural integrity (non-monotonic / non-finite) and remaining issues not explicitly waived
        post_issues = [
            i for i in run_checks(current_curve, req, self.policy)
            if not any(
                d.issue in (i.code, "*") and d.action == "trust_curve"
                for d in find_matching_decisions(self.decisions, req, issue_code=i.code)
            )
        ]
        if post_issues:
            return self._pending(req, attempted, post_issues)

        return ResolvedAntenna(
            request=req,
            status="resolved",
            curve=current_curve,
            source_used=chosen_curve.source_name,
            issues_detected=issues,
            applied_decisions=applied_decisions,
        )
