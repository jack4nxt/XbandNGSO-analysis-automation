"""Strict Pydantic configuration schemas (no implicit business defaults)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, PositiveFloat, PositiveInt, field_validator, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------- paths.yaml
class DirsConfig(StrictModel):
    outputs: str
    cache: str
    raw_inputs: str


class LayoutConfig(StrictModel):
    batch_dir: str
    pair_dir: str
    post_dir: str
    idx_width: PositiveInt
    name_sanitize_regex: str
    name_sanitize_char: str
    timestamp_format: str
    coordination_table_file: str
    validation_report_file: str
    batch_state_file: str
    preflight_dir: str
    pending_antennas_csv: str
    pending_antennas_md: str
    frozen_subdir: str
    raw_subdir: str
    post_runs_subdir: str
    pairs_subdir: str
    batch_summary_file: str
    batch_verdict_file: str
    coordination_context_file: str
    freeze_manifest_file: str
    batch_yaml_copy_file: str
    antenna_subdir: str
    libraries_subdir: str
    snapshots_subdir: str
    input_workbook_file: str
    run_yaml_file: str
    provenance_file: str


class PathsConfig(StrictModel):
    data_root_env: str
    # optional dotenv file that exports `data_root_env` (and engine variables); null = none
    env_file: Optional[str]
    dirs: DirsConfig
    layout: LayoutConfig


# --------------------------------------------------------------------------- batch.yaml
class BandSpec(StrictModel):
    name: str
    f_low_mhz: PositiveFloat
    f_high_mhz: PositiveFloat
    directions: List[Literal["DL", "UL"]]

    @model_validator(mode="after")
    def _check_bounds(self) -> "BandSpec":
        if self.f_high_mhz <= self.f_low_mhz:
            raise ValueError("f_high_mhz must be greater than f_low_mhz")
        if not self.directions:
            raise ValueError("directions must not be empty")
        return self


class CoordinationInputSpec(StrictModel):
    path: str
    kind: Literal["legacy_excel", "user_excel", "word", "pdf", "standard"]


class CoordinationSection(StrictModel):
    inputs: List[CoordinationInputSpec]
    rules: str
    decisions: Optional[str]


class TimeSection(StrictModel):
    start_utc: str
    duration_days: PositiveFloat
    coarse_step_s: PositiveFloat

    @field_validator("start_utc")
    @classmethod
    def _forbid_today_and_require_iso(cls, value: str) -> str:
        cleaned = value.strip()
        if cleaned.upper() == "TODAY":
            raise ValueError('start_utc="TODAY" is forbidden in batch mode; freeze an ISO-8601 UTC timestamp')
        try:
            datetime.fromisoformat(cleaned.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"start_utc must be ISO-8601 timestamp: {exc}") from None
        return cleaned


class AntennaSection(StrictModel):
    policy: str
    decisions: str


class EngineSection(StrictModel):
    catalog_adapter: str
    engine_adapter: str
    options: Dict[str, Any]


class RunnerSection(StrictModel):
    max_workers: PositiveInt
    executor: Literal["process", "thread"] = "process"
    resume: bool
    continue_on_error: bool


class BatchConfig(StrictModel):
    batch_id: str
    evaluated_network: str
    evaluated_role: Literal["interferer", "victim", "both"]
    bands: List[BandSpec]
    coordination: CoordinationSection
    time: TimeSection
    overrides: Optional[str]
    antenna: AntennaSection
    engine: EngineSection
    runner: RunnerSection

    @model_validator(mode="after")
    def _check_non_empty(self) -> "BatchConfig":
        if not self.bands:
            raise ValueError("bands must not be empty")
        if not self.coordination.inputs:
            raise ValueError("coordination.inputs must not be empty")
        return self


# --------------------------------------------------------------------------- coordination_rules.yaml
class ExcelParserRule(StrictModel):
    sheet: str
    header_row: PositiveInt
    first_data_row: PositiveInt
    columns: Dict[str, str]


class TableParserRule(StrictModel):
    table_index: Optional[int] = None
    columns: Dict[str, str]


class NormalizeRule(StrictModel):
    cell_separators: List[str]
    strip_suffix_patterns: List[str]
    case_insensitive: bool


class MatchRule(StrictModel):
    strategies: List[Literal["exact", "alnum_insensitive"]]
    allow_multiple_notices: bool
    prefer_associated_earth_stations: bool = True


class FilterRule(StrictModel):
    exclude_reasons: List[str]
    require_chn_stations: bool
    # Drop list rows whose "Frequency Band" text has numeric ranges and none overlaps a batch band.
    # Rows whose text cannot be parsed are kept (never dropped silently).
    band_text_prefilter: bool
    band_range_regex: str
    required_services: List[str] = []


class CoordinationRulesConfig(StrictModel):
    legacy_excel: ExcelParserRule
    user_excel: ExcelParserRule
    word: TableParserRule
    pdf: TableParserRule
    normalize: NormalizeRule
    match: MatchRule
    filter: FilterRule


class CoordinationDecisionEntry(StrictModel):
    raw_name: str
    matched_name: Optional[str]
    ntc_id: Optional[int]
    adm: Optional[str]
    action: Literal["select", "skip"]
    by: str
    date: str
    reason: str


class CoordinationDecisionsConfig(StrictModel):
    decisions: List[CoordinationDecisionEntry]


# --------------------------------------------------------------------------- antenna_policy.yaml / decisions
class ItuModelRules(StrictModel):
    required_params: Dict[str, List[str]]


class EarthStationAntennaRules(StrictModel):
    model_map: Dict[str, str]
    allow_fallback: bool

    @field_validator("allow_fallback")
    @classmethod
    def _must_be_false(cls, value: bool) -> bool:
        if value:
            raise ValueError("earth_station.allow_fallback must be false to prevent silent model substitution")
        return value


class PatternTableCfg(StrictModel):
    """Pre-digitized pattern table in the catalog database (read-only SELECT)."""

    table: str
    statuses: List[str]
    # database `source_type` value -> policy source name
    source_type_map: Dict[str, str]


class AntennaPolicyConfig(StrictModel):
    source_order: List[str]
    required_phi_max_deg: PositiveFloat
    phi_step_deg: PositiveFloat
    peak_tol_db: float
    beamwidth_tol_frac: float
    itu_model: ItuModelRules
    earth_station: EarthStationAntennaRules
    pattern_table: Optional[PatternTableCfg] = None


class DecisionKey(StrictModel):
    network: str
    beam: str
    emi_rcp: Literal["E", "R", "*"]
    role: Literal["space", "earth", "*"]
    # earth-station variant (`<ant_type>|<gain>|...`); "*" = any
    variant: str = "*"


class AntennaDecisionEntry(StrictModel):
    id: str
    key: DecisionKey
    issue: str
    action: str
    params: Dict[str, Any]
    by: str
    date: str
    reason: str


class AntennaDecisionsConfig(StrictModel):
    decisions: List[AntennaDecisionEntry]


# --------------------------------------------------------------------------- overrides.yaml
class OverrideTarget(StrictModel):
    network: str
    role: Literal["interferer", "victim", "*"]


class OverrideSelect(StrictModel):
    kind: Literal["carrier", "earth_station", "orbit", "stations"]
    beam: Optional[str] = None
    direction: Optional[Literal["DL", "UL", "*"]] = None
    station_id: Optional[str] = None
    plane_id: Optional[str] = None


class OverrideItem(StrictModel):
    id: str
    target: OverrideTarget
    select: OverrideSelect
    set: Dict[str, Any]
    delta: Dict[str, float]
    curve_policy: Optional[Literal["shift", "keep", "reject"]]
    reason: str

    @model_validator(mode="after")
    def _validate_item(self) -> "OverrideItem":
        overlap = set(self.set) & set(self.delta)
        if overlap:
            raise ValueError(f"fields cannot appear in both set and delta: {sorted(overlap)}")
        if not self.set and not self.delta:
            raise ValueError("override item must specify at least one field in set or delta")
        gmax_changed = ("sat_gmax_dbi" in self.set) or ("sat_gmax_dbi" in self.delta)
        if gmax_changed and self.curve_policy is None:
            raise ValueError("curve_policy (shift|keep|reject) is required when overriding sat_gmax_dbi")
        return self


class OverridesConfig(StrictModel):
    psd_consistency_mode: Literal["recompute_missing_or_paired", "independent"]
    items: List[OverrideItem]


# --------------------------------------------------------------------------- post.yaml
class EnginePostSection(StrictModel):
    post_yaml: str
    overrides: Dict[str, Any]


class ReportSection(StrictModel):
    enabled: bool
    org: str


class SummarySection(StrictModel):
    sort_by: List[str]
    columns: List[str]


class PostBatchConfig(StrictModel):
    post_name: str
    max_workers: PositiveInt = 4
    executor: Literal["process", "thread"] = "process"
    continue_on_error: bool = False
    engine_post: EnginePostSection
    report: ReportSection
    summary: SummarySection
