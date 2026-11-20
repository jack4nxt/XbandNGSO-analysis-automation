"""Preflight antenna & coordination readiness gatekeeper."""

from .checker import build_pair_plans, run_preflight
from .models import PairPlan, PreflightReport

__all__ = ["PairPlan", "PreflightReport", "build_pair_plans", "run_preflight"]
