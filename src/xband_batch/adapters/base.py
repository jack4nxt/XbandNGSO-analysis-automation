"""Abstract adapter protocols: the only seam between batch orchestration and external backends."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Protocol, Tuple, runtime_checkable

from ..antenna.models import AntennaKey


@dataclass
class NetworkCandidate:
    sat_name: str
    ntc_id: int
    adm: str
    orbit_type: str  # "NGSO" | "GSO"
    d_rcv: Optional[str] = None
    st_cur: Optional[str] = None


@dataclass
class CarrierDescriptor:
    carrier_id: str
    beam_name: str
    emi_rcp: str  # "E" | "R"
    direction: str  # "DL" | "UL"
    f_low_mhz: float
    f_high_mhz: float
    bandwidth_hz: float
    power_dbw: Optional[float]
    psd_dbw_hz: Optional[float]
    sat_gain_dbi: Optional[float]
    sat_pattern_id: Optional[str]
    sat_beamlet_deg: Optional[float] = None
    sat_noise_temp_k: Optional[float] = None
    services: List[str] = field(default_factory=list)
    station_ids: List[str] = field(default_factory=list)
    extra: Dict[str, Any] = field(default_factory=dict)


@dataclass
class OrbitDescriptor:
    plane_id: str
    n_sats: int
    inc_deg: float
    node_deg: float
    node_type: str  # "RAAN" | "LAN"
    argp_deg: float
    altitude_km: Optional[float]
    sma_km: Optional[float]
    ecc: float
    mean_anomalies_deg: List[float]
    lt_type: Optional[str] = None
    lt_hours: Optional[float] = None
    extra: Dict[str, Any] = field(default_factory=dict)


@dataclass
class EarthStationDescriptor:
    station_id: str
    direction: str  # "DL" | "UL"
    beam_name: str
    emi_rcp: str
    lon_deg: float
    lat_deg: float
    alt_m: float
    gmax_dbi: Optional[float]
    beamwidth_deg: Optional[float]
    ant_diam_m: Optional[float]
    ant_type: Optional[str]
    min_elev_deg: Optional[float]
    rx_noise_temp_k: Optional[float]
    country: str
    extra: Dict[str, Any] = field(default_factory=dict)


@dataclass
class NetworkExtract:
    sat_name: str
    ntc_id: int
    adm: str
    orbit_type: str
    planes: List[OrbitDescriptor] = field(default_factory=list)
    carriers: List[CarrierDescriptor] = field(default_factory=list)
    stations: List[EarthStationDescriptor] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class CatalogAdapter(Protocol):
    """Read-only lookup against an ITU space database or equivalent catalog."""

    def find_networks(
        self,
        name: str,
        strategy: str,
        adm: Optional[str] = None,
    ) -> List[NetworkCandidate]:
        """Return matching notices using the requested matching strategy."""

    def describe_network(
        self,
        ntc_id: int,
        bands: List[Dict[str, Any]],
        country_filter: Optional[str] = None,
    ) -> NetworkExtract:
        """Extract orbit, carrier, service, and earth-station summary for a single notice."""


@runtime_checkable
class EngineAdapter(Protocol):
    """Execution adapter wrapping a 1-on-1 simulation and statistics backend.

    Optional (duck-typed) method used by preflight/freeze when no explicit context is given:

        antenna_source_context(policy, options) -> dict
            providers for the antenna sources (see antenna/sources/builtins.py)
    """

    def extract_network(
        self,
        sat_name: str,
        ntc_id: Optional[int],
        options: Dict[str, Any],
    ) -> NetworkExtract:
        """Extract full simulation-ready network parameters from the backend source."""

    def build_input(
        self,
        interferer: NetworkExtract,
        victim: NetworkExtract,
        resolved_patterns: Dict[AntennaKey, Tuple[List[float], List[float]]],
        time_cfg: Dict[str, Any],
        frozen_dir: Path,
        provenance: Dict[str, Any],
        options: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Materialize a frozen pair input bundle (e.g., INPUT.xlsx + run.yaml + provenance)."""

    def run_pair(
        self,
        frozen_dir: Path,
        raw_dir: Path,
        options: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Execute 1-on-1 simulation reading exclusively from frozen_dir and writing to raw_dir."""

    def run_stats(
        self,
        raw_dir: Path,
        post_out_dir: Path,
        post_cfg: Dict[str, Any],
        report_cfg: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        """Run post-processing on raw_dir and return per-scenario/link summary rows."""
