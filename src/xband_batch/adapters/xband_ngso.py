"""Concrete adapters wiring xband_batch to the existing X-band-NGSO engine & itu_space DB."""

from __future__ import annotations

import csv
import json
import math
import shutil
import sys
import tempfile
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import yaml

from ..antenna.models import AmbiguousPatternError, AntennaKey, AntennaRequest
from ..config.schemas import AntennaPolicyConfig
from ..errors import BatchError, ConfigError
from ..preflight.checker import earth_variant
from .base import (
    CarrierDescriptor,
    EarthStationDescriptor,
    NetworkCandidate,
    NetworkExtract,
    OrbitDescriptor,
)

_DB_FACTOR = 10.0
_HZ_PER_KHZ = 1.0e3
_HZ_PER_MHZ = 1.0e6
_AP30_REF_HZ = 12.1e9
_STATION_SERVICE = {
    "C": "FSS", "I": "MSS", "W": "EESS", "M": "MetSat", "H": "SRS", "B": "BSS",
    "N": "RNSS", "K": "SOS", "D": "SOS", "R": "SOS", "T": "SOS", "U": "LMSS",
    "J": "AMSS", "G": "MMSS", "E": "SES", "S": "ISS",
}

_HOOK_LOCK = threading.Lock()
_STAGE_ROUTES: Dict[str, Path] = {}
_NEW_RUN_PATCHED = False


def _ensure_repo(options: Dict[str, Any]) -> Path:
    repo = options.get("repo_path")
    if not repo:
        raise ConfigError("engine.options", "repo_path", "repo_path is required for XBandNgso adapters")
    repo_path = Path(str(repo)).expanduser().resolve()
    src_dir = repo_path / "src"
    if not src_dir.is_dir():
        raise ConfigError("engine.options", "repo_path", f"src directory not found under {repo_path}")
    if str(src_dir) not in sys.path:
        sys.path.insert(0, str(src_dir))
    return repo_path


def _install_stage_router() -> None:
    """Thread-safe one-time hook on x_band_ngso.sim.engine.new_run to route staged batch pairs."""
    global _NEW_RUN_PATCHED
    with _HOOK_LOCK:
        if _NEW_RUN_PATCHED:
            return
        import x_band_ngso.sim.engine as sim_engine

        orig_new_run = sim_engine.new_run

        def _routed_new_run(group, input_files, outputs_root):
            with _HOOK_LOCK:
                staged = _STAGE_ROUTES.get(str(group))
            if staged is not None:
                return str(group), staged
            return orig_new_run(group, input_files, outputs_root)

        sim_engine.new_run = _routed_new_run
        _NEW_RUN_PATCHED = True


def _resolve_itu_cfg_path(options: Dict[str, Any], repo_path: Path) -> Path:
    val = options.get("itu_extract_yaml")
    return Path(str(val)).expanduser().resolve() if val else (repo_path / "configs" / "library" / "itu_extract.yaml")


def _resolve_run_template_path(options: Dict[str, Any], repo_path: Path) -> Path:
    val = options.get("run_template_yaml")
    return Path(str(val)).expanduser().resolve() if val else (repo_path / "configs" / "run_pair.yaml")


@contextmanager
def _db_conn(options: Dict[str, Any]):
    _ensure_repo(options)
    from x_band_ngso.itu.db import connection
    from x_band_ngso.errors import InputError

    try:
        with connection() as conn:
            yield conn
    except InputError as exc:
        raise BatchError(f"ITU database error: {exc}") from None


def _services_from_codes(codes: List[str]) -> List[str]:
    out = []
    for code in codes:
        c = (code or "").strip()
        if len(c) >= 2:
            out.append(_STATION_SERVICE.get(c[1], c))
    return sorted(set(out))


def _load_full_network(conn, sat_name: str, ntc_id: Optional[int], cfg, earth_radius_km: float) -> NetworkExtract:
    """Read one notice from itu_space, deduplicate via x_band_ngso, and preserve raw antenna metadata."""
    from x_band_ngso.errors import InputError
    from x_band_ngso.itu import dedupe
    from x_band_ngso.itu.db import query
    from x_band_ngso.itu.extract import fetch_raw
    from x_band_ngso.itu.lookup import find_candidates, select_notice

    try:
        if ntc_id is not None:
            rows = query(
                conn,
                "select n.ntc_id, n.adm, n.prov, n.act_code, n.st_cur, n.d_rcv, n.wic_no, "
                "n.f_int_ext, g.sat_name, g.nbr_plane from public.non_geo g "
                "join public.notice n using (ntc_id) where n.ntc_id = %s",
                [ntc_id],
            )
            if not rows:
                raise BatchError(f"ntc_id {ntc_id} not found in public.non_geo")
            selection = select_notice(conn, rows, cfg, ntc_id=ntc_id)
            chosen_name = rows[0]["sat_name"]
        else:
            candidates = find_candidates(conn, sat_name, adm=None)
            selection = select_notice(conn, candidates, cfg, ntc_id=None)
            chosen_name = candidates[0]["sat_name"]
        raw = fetch_raw(conn, selection)
        net = dedupe.build_network(chosen_name, raw, cfg, earth_radius_km)
    except InputError as exc:
        raise BatchError(str(exc)) from None

    ntc = raw["notice"]["ntc_id"]
    beam_extra_rows = query(
        conn,
        "select sb.beam_name, sb.emi_rcp, sb.beamlet, sb.pattern_id, "
        "a.pattern as sat_pattern, a.coefa, a.coefb, a.coefc, a.coefd, a.phi1 "
        "from public.s_beam sb "
        "left join public.ant_type a on a.pattern_id = sb.pattern_id "
        "where sb.ntc_id = %s and sb.act_code is distinct from 'S'",
        [ntc],
    )
    beam_extra = {(r["beam_name"], r["emi_rcp"]): r for r in beam_extra_rows}

    gids = [g["grp_id"] for g in raw["groups"]]
    srv_rows = query(
        conn,
        "select grp_id, stn_cls from public.srv_cls where grp_id = any(%s) and stn_cls is not null",
        [gids],
    ) if gids else []
    srv_by_grp: Dict[int, List[str]] = {}
    for sr in srv_rows:
        srv_by_grp.setdefault(sr["grp_id"], []).append(sr["stn_cls"])

    stn_extra_rows = query(
        conn,
        "select e.grp_id, e.stn_name, e.ant_diam, a.pattern as ant_pattern, "
        "a.coefa, a.coefb, a.coefc, a.coefd, a.phi1 "
        "from public.e_as_stn e "
        "left join public.ant_type a on a.pattern_id = e.pattern_id "
        "where e.grp_id = any(%s) and e.act_code is distinct from 'S'",
        [gids],
    ) if gids else []
    stn_extra: Dict[Tuple[int, str], dict] = {}
    for er in stn_extra_rows:
        stn_extra[(er["grp_id"], (er["stn_name"] or "").strip())] = er

    # Fetch stored raster path hints from xband_antenna_pattern (if any) for diagnostics
    img_rows = query(
        conn,
        "select beam_name, emi_rcp, role, path, status from public.xband_antenna_pattern "
        "where upper(sat_name) = upper(%s) and path is not null",
        [net.name],
    )
    raw_img_by_key = {(r["beam_name"], r["emi_rcp"], r["role"]): r["path"] for r in img_rows if r.get("path")}

    planes = [
        OrbitDescriptor(
            plane_id=f"P{p.orb_id}",
            n_sats=p.n_sats,
            inc_deg=p.inc_deg,
            node_deg=p.node_deg,
            node_type=p.node_type,
            argp_deg=p.argp_deg,
            altitude_km=p.altitude_km,
            sma_km=p.sma_km,
            ecc=p.ecc,
            mean_anomalies_deg=list(p.mean_anomalies_deg),
            lt_type=p.lt_type,
            lt_hours=p.lt_hours,
            extra={"orb_id": p.orb_id, "duplicates": list(p.duplicates), "notes": p.notes},
        )
        for p in net.planes
    ]

    carriers: List[CarrierDescriptor] = []
    stations: List[EarthStationDescriptor] = []
    seen_station_keys = set()

    for c in net.carriers:
        be = beam_extra.get((c.beam_name, c.emi_rcp), {})
        sat_params: Dict[str, Any] = {}
        for col in ("coefa", "coefb", "coefc", "coefd", "phi1"):
            if be.get(col) is not None:
                key_name = "phi1_deg" if col == "phi1" else f"coef_{col[-1]}"
                sat_params[key_name] = float(be[col])

        codes = [code for gid in c.grp_ids for code in srv_by_grp.get(gid, [])]
        services = _services_from_codes(codes)

        st_ids: List[str] = []
        for st in c.stations:
            st_ids.append(st.name)
            match_extra = None
            for gid in c.grp_ids:
                match_extra = stn_extra.get((gid, st.name))
                if match_extra is not None:
                    break
            ant_diam = None
            st_params: Dict[str, Any] = {}
            if match_extra is not None:
                if match_extra.get("ant_diam") is not None:
                    ant_diam = float(match_extra["ant_diam"])
                for col in ("coefa", "coefb", "coefc", "coefd", "phi1"):
                    if match_extra.get(col) is not None:
                        kname = "phi1_deg" if col == "phi1" else f"coef_{col[-1]}"
                        st_params[kname] = float(match_extra[col])

            skey = (st.name, c.direction, c.beam_name, c.emi_rcp)
            if skey not in seen_station_keys:
                seen_station_keys.add(skey)
                stations.append(
                    EarthStationDescriptor(
                        station_id=st.name,
                        direction=c.direction,
                        beam_name=c.beam_name,
                        emi_rcp=c.emi_rcp,
                        lon_deg=st.lon_deg,
                        lat_deg=st.lat_deg,
                        alt_m=st.alt_m,
                        gmax_dbi=st.gain_dbi,
                        beamwidth_deg=st.beamwidth_deg,
                        ant_diam_m=ant_diam,
                        ant_type=st.ant_type,  # raw unmapped ant_type (never silently defaulted!)
                        min_elev_deg=cfg.earth_station.default_min_elev_deg,
                        rx_noise_temp_k=st.noise_temp_k,
                        country=cfg.earth_station.country,
                        extra={
                            "aliases": list(st.aliases),
                            "model_params": st_params,
                            "raw_image_path": raw_img_by_key.get((c.beam_name, c.emi_rcp, "earth")),
                        },
                    )
                )

        carriers.append(
            CarrierDescriptor(
                carrier_id=c.carrier_id,
                beam_name=c.beam_name,
                emi_rcp=c.emi_rcp,
                direction=c.direction,
                f_low_mhz=c.f_low_mhz,
                f_high_mhz=c.f_high_mhz,
                bandwidth_hz=c.bandwidth_hz,
                power_dbw=c.power_dbw,
                psd_dbw_hz=c.psd_dbw_hz,
                sat_gain_dbi=c.sat_gain_dbi,
                sat_pattern_id=be.get("sat_pattern"),
                sat_beamlet_deg=float(be["beamlet"]) if be.get("beamlet") is not None else None,
                sat_noise_temp_k=c.sat_noise_temp_k,
                services=services,
                station_ids=st_ids,
                extra={
                    "grp_ids": list(c.grp_ids),
                    "notes": c.notes,
                    "model_params": sat_params,
                    "raw_image_path": raw_img_by_key.get((c.beam_name, c.emi_rcp, "space")),
                },
            )
        )

    return NetworkExtract(
        sat_name=net.name,
        ntc_id=ntc,
        adm=str(net.notice.get("adm") or ""),
        orbit_type="NGSO",
        planes=planes,
        carriers=carriers,
        stations=stations,
        metadata={
            "notice": dict(net.notice),
            "nbr_plane_declared": net.nbr_plane_declared,
            "removed_duplicates": list(net.removed),
            "skipped": list(net.skipped),
            "warnings": list(net.warnings),
            "other_notices": [
                {k: (str(v) if k == "d_rcv" and v is not None else v) for k, v in o.items()}
                for o in selection.others
            ],
        },
    )


def _extract_model_params(row: Dict[str, Any]) -> Dict[str, Any]:
    params: Dict[str, Any] = {}
    for col in ("coefa", "coefb", "coefc", "coefd", "phi1"):
        if row.get(col) is not None:
            key_name = "phi1_deg" if col == "phi1" else f"coef_{col[-1]}"
            params[key_name] = float(row[col])
    return params


def _merge_sibling_notice_stations(
    conn: Any,
    net_ext: NetworkExtract,
    country_filter: str,
    itu_cfg: Any,
    raw_img_by_key: Dict[Tuple[str, str, str], str] | None = None,
) -> NetworkExtract:
    """Union & deduplicate associated specific earth stations across sibling notices of the same network."""
    from x_band_ngso.itu.db import query

    if not net_ext.carriers:
        return net_ext

    siblings = query(
        conn,
        "select n.ntc_id "
        "from public.non_geo g join public.notice n using (ntc_id) "
        "where n.ntc_id <> %s "
        "  and upper(trim(g.sat_name)) = upper(trim(%s)) "
        "  and coalesce(n.adm, '') = %s "
        "  and n.act_code is distinct from 'S' "
        "order by n.d_rcv desc nulls last, n.ntc_id desc",
        [net_ext.ntc_id, net_ext.sat_name, net_ext.adm],
    )
    sib_ids = [int(r["ntc_id"]) for r in siblings]
    if not sib_ids:
        return net_ext

    sib_rows = query(
        conn,
        "select g.ntc_id, g.grp_id, g.beam_name, g.emi_rcp, g.freq_min, g.freq_max, g.noise_t as grp_noise_t, "
        "       e.seq_no, e.stn_name, e.long_dec, e.lat_dec, e.ant_alt, e.gain, e.bmwdth, "
        "       e.ant_diam, e.noise_t, e.ctry, "
        "       a.pattern as ant_type, a.coefa, a.coefb, a.coefc, a.coefd, a.phi1 "
        "from public.grp g "
        "join public.e_as_stn e on e.grp_id = g.grp_id "
        "left join public.ant_type a on a.pattern_id = e.pattern_id "
        "where g.ntc_id = any(%s) "
        "  and g.act_code is distinct from 'S' "
        "  and g.freq_min is not null and g.freq_max is not null "
        "  and e.act_code is distinct from 'S' "
        "  and e.stn_type = 'S' "
        "  and e.long_dec is not null and e.lat_dec is not null "
        "  and e.ctry = %s "
        "order by array_position(%s::bigint[], g.ntc_id::bigint), g.grp_id, e.seq_no",
        [sib_ids, country_filter, sib_ids],
    )
    if not sib_rows:
        return net_ext

    rows_by_sib: Dict[int, List[dict]] = {}
    for r in sib_rows:
        rows_by_sib.setdefault(int(r["ntc_id"]), []).append(r)

    pos_tol = float(itu_cfg.earth_station.position_tolerance_deg)
    seen_station_keys = {(s.station_id, s.direction, s.beam_name, s.emi_rcp) for s in net_ext.stations}
    merged_notices: set[int] = set()

    for c in net_ext.carriers:
        carrier_stations = [
            s for s in net_ext.stations
            if s.beam_name == c.beam_name and s.emi_rcp == c.emi_rcp and s.station_id in c.station_ids
        ]
        for sib_ntc in sib_ids:
            cand_rows = [
                r for r in rows_by_sib.get(sib_ntc, [])
                if r["emi_rcp"] == c.emi_rcp
                and float(r["freq_max"]) > c.f_low_mhz
                and float(r["freq_min"]) < c.f_high_mhz
            ]
            if not cand_rows:
                continue
            beam_matched = [r for r in cand_rows if r["beam_name"] == c.beam_name]
            chosen_rows = beam_matched if beam_matched else cand_rows
            for r in chosen_rows:
                st_name = (r["stn_name"] or "").strip()
                if not st_name:
                    continue
                lon, lat = float(r["long_dec"]), float(r["lat_dec"])
                is_dup = any(
                    ex.station_id.strip().upper() == st_name.upper()
                    or (abs(ex.lon_deg - lon) <= pos_tol and abs(ex.lat_deg - lat) <= pos_tol)
                    for ex in carrier_stations
                )
                if is_dup:
                    continue
                gain = (
                    float(r["gain"])
                    if r.get("gain") is not None
                    else itu_cfg.earth_station.default_gain_dbi
                )
                noise_t = (
                    float(r["noise_t"])
                    if r.get("noise_t") is not None
                    else (
                        float(r["grp_noise_t"])
                        if c.emi_rcp == "E" and r.get("grp_noise_t") is not None
                        else None
                    )
                )
                st_desc = EarthStationDescriptor(
                    station_id=st_name,
                    direction=c.direction,
                    beam_name=c.beam_name,
                    emi_rcp=c.emi_rcp,
                    lon_deg=lon,
                    lat_deg=lat,
                    alt_m=float(r["ant_alt"]) if r.get("ant_alt") is not None else float(itu_cfg.earth_station.default_alt_m),
                    gmax_dbi=float(gain) if gain is not None else None,
                    beamwidth_deg=float(r["bmwdth"]) if r.get("bmwdth") is not None else None,
                    ant_diam_m=float(r["ant_diam"]) if r.get("ant_diam") is not None else None,
                    ant_type=r.get("ant_type"),
                    min_elev_deg=float(itu_cfg.earth_station.default_min_elev_deg),
                    rx_noise_temp_k=noise_t,
                    country=country_filter,
                    extra={
                        "aliases": [],
                        "model_params": _extract_model_params(r),
                        "raw_image_path": (raw_img_by_key or {}).get((c.beam_name, c.emi_rcp, "earth")),
                        "source_ntc_id": sib_ntc,
                    },
                )
                c.station_ids.append(st_name)
                carrier_stations.append(st_desc)
                skey = (st_name, c.direction, c.beam_name, c.emi_rcp)
                if skey not in seen_station_keys:
                    seen_station_keys.add(skey)
                    net_ext.stations.append(st_desc)
                merged_notices.add(sib_ntc)

    if merged_notices:
        net_ext.metadata["merged_station_notices"] = sorted(merged_notices)
    return net_ext


class XBandNgsoCatalogAdapter:
    """Read-only CatalogAdapter targeting PostgreSQL `itu_space` via x_band_ngso.itu."""

    def __init__(self, options: Dict[str, Any] | None = None):
        self.options = options or {}

    def find_networks(
        self,
        name: str,
        strategy: str,
        adm: Optional[str] = None,
    ) -> List[NetworkCandidate]:
        repo_path = _ensure_repo(self.options)
        from x_band_ngso.itu.db import query
        from x_band_ngso.settings import load_itu_extract

        itu_cfg = load_itu_extract(_resolve_itu_cfg_path(self.options, repo_path))
        xb_lo, xb_hi = itu_cfg.x_band.f_low_mhz, itu_cfg.x_band.f_high_mhz

        if strategy == "exact":
            cond = "upper(trim(sat_name)) = upper(trim(%s))"
        elif strategy == "alnum_insensitive":
            cond = "regexp_replace(upper(sat_name), '[^A-Z0-9]', '', 'g') = regexp_replace(upper(%s), '[^A-Z0-9]', '', 'g')"
        else:
            raise ConfigError("coordination_rules", "match.strategies", f"unsupported strategy {strategy!r}")

        adm_sql = " and n.adm = %s" if adm else ""
        x_beam_exists = (
            "and exists (select 1 from public.s_beam sb where sb.ntc_id = n.ntc_id "
            "and sb.act_code is distinct from 'S' "
            "and sb.freq_min is not null and sb.freq_max is not null "
            "and sb.freq_max > %s and sb.freq_min < %s)"
        )
        sql = (
            "select n.ntc_id, n.adm, n.d_rcv, n.st_cur, g.sat_name, 'NGSO'::text as orbit_type "
            "from public.non_geo g join public.notice n using (ntc_id) "
            f"where n.act_code is distinct from 'S' and {cond}{adm_sql} {x_beam_exists} "
            "union all "
            "select n.ntc_id, n.adm, n.d_rcv, n.st_cur, g.sat_name, 'GSO'::text as orbit_type "
            "from public.geo g join public.notice n using (ntc_id) "
            f"where n.act_code is distinct from 'S' and {cond}{adm_sql} "
            "order by orbit_type desc, d_rcv desc nulls last, ntc_id desc"
        )
        params = [name] + ([adm] if adm else []) + [xb_lo, xb_hi] + [name] + ([adm] if adm else [])
        with _db_conn(self.options) as conn:
            rows = query(conn, sql, params)
        return [
            NetworkCandidate(
                sat_name=r["sat_name"].strip(),
                ntc_id=int(r["ntc_id"]),
                adm=str(r["adm"] or ""),
                orbit_type=r["orbit_type"],
                d_rcv=str(r["d_rcv"]) if r.get("d_rcv") is not None else None,
                st_cur=r.get("st_cur"),
            )
            for r in rows
        ]

    def describe_network(
        self,
        ntc_id: int,
        bands: List[Dict[str, Any]],
        country_filter: Optional[str] = None,
    ) -> NetworkExtract:
        repo_path = _ensure_repo(self.options)
        from x_band_ngso.itu.db import query
        from x_band_ngso.settings import load_itu_extract

        itu_cfg = load_itu_extract(_resolve_itu_cfg_path(self.options, repo_path))
        with _db_conn(self.options) as conn:
            gso = query(
                conn,
                "select g.sat_name, n.adm from public.geo g join public.notice n using (ntc_id) where n.ntc_id = %s",
                [ntc_id],
            )
            if gso:
                return NetworkExtract(
                    sat_name=gso[0]["sat_name"].strip(),
                    ntc_id=ntc_id,
                    adm=str(gso[0]["adm"] or ""),
                    orbit_type="GSO",
                )

            ng = query(
                conn,
                "select g.sat_name, n.adm from public.non_geo g join public.notice n using (ntc_id) where n.ntc_id = %s",
                [ntc_id],
            )
            if not ng:
                raise BatchError(f"ntc_id {ntc_id} not found in non_geo or geo")

            # Lightweight summary for classification (all groups overlapping any configured band)
            band_conds = " or ".join("(grp.freq_max > %s and grp.freq_min < %s)" for _ in bands)
            band_params = [v for b in bands for v in (b["f_low_mhz"], b["f_high_mhz"])]
            grp_rows = query(
                conn,
                "select grp.grp_id, grp.beam_name, grp.emi_rcp, grp.freq_min, grp.freq_max, grp.bdwdth "
                "from public.grp grp "
                f"where grp.ntc_id = %s and grp.act_code is distinct from 'S' "
                f"and grp.freq_min is not null and grp.freq_max is not null and ({band_conds})",
                [ntc_id] + band_params,
            )
            gids = [r["grp_id"] for r in grp_rows]
            if not gids:
                return NetworkExtract(
                    sat_name=ng[0]["sat_name"].strip(),
                    ntc_id=ntc_id,
                    adm=str(ng[0]["adm"] or ""),
                    orbit_type="NGSO",
                )

            ctry = country_filter or itu_cfg.earth_station.country
            st_rows = query(
                conn,
                "select e.grp_id, e.stn_name, e.long_dec, e.lat_dec, e.ant_alt, e.gain, e.bmwdth, "
                "e.ant_diam, e.noise_t, e.ctry, a.pattern as ant_type "
                "from public.e_as_stn e left join public.ant_type a on a.pattern_id = e.pattern_id "
                "where e.grp_id = any(%s) and e.act_code is distinct from 'S' "
                "and e.stn_type = 'S' "
                "and e.long_dec is not null and e.lat_dec is not null and e.ctry = %s",
                [gids, ctry],
            )
            st_by_grp: Dict[int, List[dict]] = {}
            for s in st_rows:
                st_by_grp.setdefault(s["grp_id"], []).append(s)

            srv_rows = query(
                conn,
                "select grp_id, stn_cls from public.srv_cls where grp_id = any(%s) and stn_cls is not null",
                [gids],
            )
            srv_by_grp: Dict[int, List[str]] = {}
            for sr in srv_rows:
                srv_by_grp.setdefault(sr["grp_id"], []).append(sr["stn_cls"])

        carriers: List[CarrierDescriptor] = []
        stations: List[EarthStationDescriptor] = []
        seen_stations = set()
        direction_map = {"E": "DL", "R": "UL"}

        for g in grp_rows:
            emi = g["emi_rcp"]
            if emi not in direction_map:
                continue
            direction = direction_map[emi]
            sts = st_by_grp.get(g["grp_id"], [])
            st_ids = []
            for s in sts:
                name = (s["stn_name"] or "").strip()
                st_ids.append(name)
                if (name, direction, g["beam_name"], emi) not in seen_stations:
                    seen_stations.add((name, direction, g["beam_name"], emi))
                    stations.append(
                        EarthStationDescriptor(
                            station_id=name,
                            direction=direction,
                            beam_name=g["beam_name"],
                            emi_rcp=emi,
                            lon_deg=float(s["long_dec"]),
                            lat_deg=float(s["lat_dec"]),
                            alt_m=float(s["ant_alt"]) if s.get("ant_alt") is not None else 0.0,
                            gmax_dbi=float(s["gain"]) if s.get("gain") is not None else None,
                            beamwidth_deg=float(s["bmwdth"]) if s.get("bmwdth") is not None else None,
                            ant_diam_m=float(s["ant_diam"]) if s.get("ant_diam") is not None else None,
                            ant_type=s.get("ant_type"),
                            min_elev_deg=itu_cfg.earth_station.default_min_elev_deg,
                            rx_noise_temp_k=float(s["noise_t"]) if s.get("noise_t") is not None else None,
                            country=ctry,
                        )
                    )
            bw_hz = (
                float(g["bdwdth"]) * _HZ_PER_KHZ
                if g.get("bdwdth")
                else (float(g["freq_max"]) - float(g["freq_min"])) * _HZ_PER_MHZ
            )
            carriers.append(
                CarrierDescriptor(
                    carrier_id=f"{g['beam_name']}-{direction}-{g['grp_id']}",
                    beam_name=g["beam_name"],
                    emi_rcp=emi,
                    direction=direction,
                    f_low_mhz=float(g["freq_min"]),
                    f_high_mhz=float(g["freq_max"]),
                    bandwidth_hz=bw_hz,
                    power_dbw=None,
                    psd_dbw_hz=None,
                    sat_gain_dbi=None,
                    sat_pattern_id=None,
                    services=_services_from_codes(srv_by_grp.get(g["grp_id"], [])),
                    station_ids=st_ids,
                )
            )

        net_ext = NetworkExtract(
            sat_name=ng[0]["sat_name"].strip(),
            ntc_id=ntc_id,
            adm=str(ng[0]["adm"] or ""),
            orbit_type="NGSO",
            carriers=carriers,
            stations=stations,
        )
        if carriers and self.options.get("union_earth_stations_across_notices", True):
            with _db_conn(self.options) as conn:
                _merge_sibling_notice_stations(conn, net_ext, ctry, itu_cfg)
        return net_ext


class XBandNgsoEngineAdapter:
    """EngineAdapter wrapping x_band_ngso workbook assembly, Engine, and run_post."""

    def __init__(self, options: Dict[str, Any] | None = None):
        self.options = options or {}

    def extract_network(
        self,
        sat_name: str,
        ntc_id: Optional[int],
        options: Dict[str, Any],
    ) -> NetworkExtract:
        opts = {**self.options, **(options or {})}
        repo_path = _ensure_repo(opts)
        from x_band_ngso.settings import load_constants, load_itu_extract

        itu_cfg = load_itu_extract(_resolve_itu_cfg_path(opts, repo_path))
        constants = load_constants(repo_path / "configs" / "library" / "model_constants.yaml")
        radius = constants.earth.altitude_reference_radius_km
        with _db_conn(opts) as conn:
            net_ext = _load_full_network(conn, sat_name, ntc_id, itu_cfg, radius)
            if opts.get("union_earth_stations_across_notices", True):
                ctry = str(opts.get("country_filter") or itu_cfg.earth_station.country)
                _merge_sibling_notice_stations(conn, net_ext, ctry, itu_cfg)
            return net_ext

    def antenna_source_context(self, policy: AntennaPolicyConfig, options: Dict[str, Any]) -> Dict[str, Any]:
        opts = {**self.options, **(options or {})}
        repo_path = _ensure_repo(opts)
        from x_band_ngso.itu.antenna_image import decode_vector_pattern, _fold_symmetric
        from x_band_ngso.itu.db import query
        from x_band_ngso.itu.reference_pattern import pattern_gain
        from x_band_ngso.settings import load_constants, load_itu_extract

        itu_cfg = load_itu_extract(_resolve_itu_cfg_path(opts, repo_path))
        constants = load_constants(repo_path / "configs" / "library" / "model_constants.yaml")
        speed_c = constants.physics.speed_of_light_m_s

        def _query_pattern_table(req: AntennaRequest, target_source: str) -> Optional[Tuple[List[float], List[float]]]:
            pt = policy.pattern_table
            if pt is None:
                return None
            matched_types = [k for k, v in pt.source_type_map.items() if v == target_source]
            if not matched_types:
                return None
            with _db_conn(opts) as conn:
                rows = query(
                    conn,
                    f"select gain_pattern, phi_step_deg, gmax_dbi, source_type, note "
                    f"from {pt.table} "
                    "where upper(sat_name) = upper(%s) and beam_name = %s and emi_rcp = %s "
                    "and role = %s and status = any(%s) and source_type = any(%s)",
                    [req.network, req.beam, req.emi_rcp, req.role, pt.statuses, matched_types],
                )
            valid = [r for r in rows if r.get("gain_pattern")]
            if not valid:
                return None
            if len(valid) > 1:
                first = [round(float(x), 4) for x in valid[0]["gain_pattern"]]
                for other in valid[1:]:
                    if [round(float(x), 4) for x in other["gain_pattern"]] != first:
                        raise AmbiguousPatternError(
                            f"{pt.table} holds {len(valid)} distinct {target_source} curves for {req.key}"
                        )
            r = valid[0]
            step = float(r.get("phi_step_deg") or policy.phi_step_deg)
            gains = [float(x) for x in r["gain_pattern"]]
            phis = [round(i * step, 6) for i in range(len(gains))]
            return phis, gains

        def gims_vector_provider(req: AntennaRequest) -> Optional[Tuple[List[float], List[float]]]:
            if req.role == "space" and req.ntc_id:
                with _db_conn(opts) as conn:
                    rows = query(
                        conn,
                        "select d.data_id, x.data_block "
                        "from gims.ngeo_diag d join gims.ngeo_data x using (data_id) "
                        "where d.ntc_id = %s and d.beam_name = %s and d.emi_rcp = %s "
                        "and d.diag_type = 'SSPAT' and x.data_type = 2 "
                        "order by d.diag_no, d.diag_seq_no",
                        [req.ntc_id, req.beam, req.emi_rcp],
                    )
                if rows:
                    decoded = []
                    for r in rows:
                        vec = decode_vector_pattern(bytes(r["data_block"]))
                        if vec is not None:
                            theta, gain, _axes = vec
                            order = np.argsort(theta)
                            th, ga = theta[order], gain[order]
                            rel = th - th[int(np.argmax(ga))]
                            dig_cfg = itu_cfg.digitize.model_copy(update={"phi_step_deg": policy.phi_step_deg})
                            phi, folded = _fold_symmetric(rel, ga, float(ga.min()), dig_cfg, 360.0)
                            decoded.append((phi.tolist(), folded.tolist()))
                    if len(decoded) > 1:
                        ref = np.asarray(decoded[0][1])
                        for _, g in decoded[1:]:
                            if not np.allclose(ref, np.asarray(g), atol=1e-3):
                                raise AmbiguousPatternError(
                                    f"gims.ngeo_diag has {len(decoded)} distinct vector curves for {req.key}"
                                )
                    if decoded:
                        return decoded[0]
            return _query_pattern_table(req, "gims_vector")

        def sheet5_provider(req: AntennaRequest) -> Optional[Tuple[List[float], List[float]]]:
            return _query_pattern_table(req, "sheet5_discrete")

        def gims_figure_provider(req: AntennaRequest) -> Optional[Tuple[List[float], List[float]]]:
            return _query_pattern_table(req, "gims_figure_verified")

        def model_evaluator(
            model_id: str,
            req: AntennaRequest,
            pol: AntennaPolicyConfig,
            params: Dict[str, Any],
        ) -> Tuple[List[float], List[float]]:
            phi_grid = np.arange(0.0, pol.required_phi_max_deg + 1e-9, pol.phi_step_deg)
            gmax = float(params["gmax_dbi"])
            d_lambda = params.get("d_lambda")
            if model_id == "REC-1213" and d_lambda is None and params.get("ant_diam_m"):
                d_lambda = float(params["ant_diam_m"]) * _AP30_REF_HZ / speed_c
            gain, _notes = pattern_gain(
                phi_grid,
                gmax,
                model_id,
                coef_a=params.get("coef_a"),
                coef_b=params.get("coef_b"),
                coef_c=params.get("coef_c"),
                coef_d=params.get("coef_d"),
                phi1_deg=params.get("phi1_deg"),
                altitude_km=params.get("altitude_km"),
                d_lambda=d_lambda,
                phi0_deg=params.get("beamlet"),
            )
            return phi_grid.tolist(), gain.tolist()

        return {
            "gims_vector_provider": gims_vector_provider,
            "sheet5_provider": sheet5_provider,
            "gims_figure_provider": gims_figure_provider,
            "model_evaluator": model_evaluator,
        }

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
        opts = {**self.options, **(options or {})}
        repo_path = _ensure_repo(opts)
        from x_band_ngso.io.template import build_workbook
        from x_band_ngso.itu.model import Carrier, Network, Plane, Station
        from x_band_ngso.itu.sunsync import apply_local_time
        from x_band_ngso.itu.workbook import assemble, filter_stations, plan_scenarios, reconcile, sanitize
        from x_band_ngso.settings import load_itu_extract

        itu_cfg = load_itu_extract(_resolve_itu_cfg_path(opts, repo_path))
        lfiles = opts.get("layout_files", {})
        ant_sub = lfiles.get("antenna_subdir", "antenna")
        lib_sub = lfiles.get("libraries_subdir", "libraries")
        wb_name = lfiles.get("input_workbook_file", "INPUT.xlsx")
        run_yaml_name = lfiles.get("run_yaml_file", "run.yaml")
        prov_name = lfiles.get("provenance_file", "provenance.json")

        def _to_itu_network(ext: NetworkExtract) -> Network:
            planes = [
                Plane(
                    orb_id=int(p.extra.get("orb_id") or idx),
                    n_sats=p.n_sats,
                    inc_deg=p.inc_deg,
                    node_deg=p.node_deg,
                    node_type=p.node_type,
                    argp_deg=p.argp_deg,
                    altitude_km=p.altitude_km,
                    sma_km=p.sma_km,
                    ecc=p.ecc,
                    mean_anomalies_deg=list(p.mean_anomalies_deg),
                    lt_type=p.lt_type,
                    lt_hours=p.lt_hours,
                    duplicates=list(p.extra.get("duplicates", [])),
                    notes=str(p.extra.get("notes", "")),
                )
                for idx, p in enumerate(ext.planes, start=1)
            ]
            st_by_key = {(s.station_id, s.direction, s.beam_name, s.emi_rcp): s for s in ext.stations}
            carriers = []
            for c in ext.carriers:
                c_sts = []
                for sid in c.station_ids:
                    sd = st_by_key.get((sid, c.direction, c.beam_name, c.emi_rcp))
                    if sd is None:
                        continue
                    es_pat_id = f"ESPAT_{sanitize(ext.sat_name)}__{sanitize(c.beam_name)}__{c.emi_rcp}__{sanitize(sd.station_id)}"
                    c_sts.append(
                        Station(
                            name=sd.station_id,
                            lon_deg=sd.lon_deg,
                            lat_deg=sd.lat_deg,
                            alt_m=sd.alt_m,
                            gain_dbi=float(sd.gmax_dbi or 0.0),
                            beamwidth_deg=sd.beamwidth_deg,
                            noise_temp_k=sd.rx_noise_temp_k,
                            ant_type=sd.ant_type,
                            antenna_model=es_pat_id,  # frozen ANTENNA_TABLE id (populated for participating pairs below)
                            aliases=list(sd.extra.get("aliases", [])),
                        )
                    )
                carriers.append(
                    Carrier(
                        beam_name=c.beam_name,
                        emi_rcp=c.emi_rcp,
                        direction=c.direction,
                        f_low_mhz=c.f_low_mhz,
                        f_high_mhz=c.f_high_mhz,
                        bandwidth_hz=c.bandwidth_hz,
                        power_dbw=float(c.power_dbw),
                        psd_dbw_hz=float(c.psd_dbw_hz),
                        sat_gain_dbi=float(c.sat_gain_dbi),
                        sat_noise_temp_k=c.sat_noise_temp_k,
                        stations=c_sts,
                        grp_ids=list(c.extra.get("grp_ids", [0])),
                        carrier_id=c.carrier_id,
                        notes=str(c.extra.get("notes", "")),
                    )
                )
            notice = dict(ext.metadata.get("notice") or {"ntc_id": ext.ntc_id, "adm": ext.adm, "d_rcv": None})
            return Network(
                name=ext.sat_name,
                notice=notice,
                nbr_plane_declared=ext.metadata.get("nbr_plane_declared"),
                planes=planes,
                carriers=carriers,
                warnings=list(ext.metadata.get("warnings", [])),
                removed=list(ext.metadata.get("removed_duplicates", [])),
                skipped=list(ext.metadata.get("skipped", [])),
            )

        net_a = _to_itu_network(interferer)
        net_b = _to_itu_network(victim)
        filter_stations(net_a, None)
        filter_stations(net_b, None)
        pairs, warns = plan_scenarios(net_a, net_b)
        if not pairs:
            raise BatchError(f"no overlapping X-band scenarios between {interferer.sat_name} and {victim.sat_name}")

        ant_dir = frozen_dir / ant_sub
        ant_dir.mkdir(parents=True, exist_ok=True)
        tables = {}
        extra_antenna_rows: List[dict] = []
        seen_es_patterns = set()

        for side, net in (("A", net_a), ("B", net_b)):
            for ca, cb in pairs:
                c = ca if side == "A" else cb
                if (side, c.carrier_id) not in tables:
                    skey = (net.name, c.beam_name, c.emi_rcp, "space", "")
                    if skey not in resolved_patterns:
                        raise BatchError(f"missing frozen space antenna curve for {skey}")
                    phi, gain = resolved_patterns[skey]
                    pid = f"{itu_cfg.workbook.sat_antenna_prefix}_{sanitize(net.name)}__{sanitize(c.beam_name)}__{c.emi_rcp}"
                    _write_curve_csv(ant_dir / f"{pid}.csv", phi, gain)
                    tables[(side, c.carrier_id)] = (pid, np.asarray(phi), np.asarray(gain), {"frozen": True})

                # Also freeze earth-station patterns into ANTENNA_TABLE + CSV
                ext_side = interferer if side == "A" else victim
                st_by_key = {(s.station_id, s.direction, s.beam_name, s.emi_rcp): s for s in ext_side.stations}
                for st in c.stations:
                    if st.antenna_model in seen_es_patterns:
                        continue
                    seen_es_patterns.add(st.antenna_model)
                    sd = st_by_key[(st.name, c.direction, c.beam_name, c.emi_rcp)]
                    es_key = (net.name, c.beam_name, c.emi_rcp, "earth", earth_variant(sd))
                    if es_key not in resolved_patterns:
                        raise BatchError(f"missing frozen earth-station antenna curve for {es_key}")
                    ephi, egain = resolved_patterns[es_key]
                    _write_curve_csv(ant_dir / f"{st.antenna_model}.csv", ephi, egain)
                    extra_antenna_rows.extend(
                        {"pattern_id": st.antenna_model, "phi_deg": round(float(p), 4), "gain_dbi": round(float(g), 4)}
                        for p, g in zip(ephi, egain)
                    )

        start_utc = str(time_cfg["start_utc"]).rstrip("Z")
        apply_local_time(net_a, start_utc, itu_cfg)
        apply_local_time(net_b, start_utc, itu_cfg)

        data, meta = assemble(net_a, net_b, pairs, tables, itu_cfg, start_utc)
        data["ANTENNA_TABLE"].extend(extra_antenna_rows)
        wb_path = build_workbook(frozen_dir / wb_name, data)
        summary = reconcile(wb_path, {"A": net_a, "B": net_b}, data)

        # Snapshot libraries and build frozen RunConfig YAML
        run_tmpl = _resolve_run_template_path(opts, repo_path)
        tmpl_dict = yaml.safe_load(run_tmpl.read_text(encoding="utf-8"))
        lib_dir = frozen_dir / lib_sub
        lib_dir.mkdir(parents=True, exist_ok=True)
        frozen_libs = {}
        for k, rel in tmpl_dict["libraries"].items():
            src_lib = (repo_path / "configs" / rel).resolve()
            dst_lib = lib_dir / src_lib.name
            shutil.copy2(src_lib, dst_lib)
            frozen_libs[k] = str(dst_lib)

        tmpl_dict["run_name"] = f"{sanitize(interferer.sat_name)}_vs_{sanitize(victim.sat_name)}"
        tmpl_dict["input_excel"] = str(wb_path.resolve())
        tmpl_dict["libraries"] = frozen_libs
        tmpl_dict["time"]["start_utc"] = start_utc
        tmpl_dict["time"]["duration_days"] = float(time_cfg["duration_days"])
        tmpl_dict["stepping"]["coarse_step_s"] = float(time_cfg["coarse_step_s"])

        run_yaml_path = frozen_dir / run_yaml_name
        run_yaml_path.write_text(yaml.safe_dump(tmpl_dict, sort_keys=False, allow_unicode=True), encoding="utf-8")

        full_prov = {
            **provenance,
            "workbook": wb_path.name,
            "workbook_summary": summary,
            "system_ids": meta["system_ids"],
            "warnings": warns + net_a.warnings + net_b.warnings,
        }
        (frozen_dir / prov_name).write_text(
            json.dumps(full_prov, indent=2, default=str, ensure_ascii=False),
            encoding="utf-8",
        )
        return {"workbook": str(wb_path), "run_yaml": str(run_yaml_path)}

    def run_pair(
        self,
        frozen_dir: Path,
        raw_dir: Path,
        options: Dict[str, Any],
    ) -> Dict[str, Any]:
        opts = {**self.options, **(options or {})}
        _ensure_repo(opts)
        _install_stage_router()
        from x_band_ngso.settings.run_schema import RunConfig
        from x_band_ngso.sim import Engine

        lfiles = opts.get("layout_files", {})
        run_yaml_path = frozen_dir / lfiles.get("run_yaml_file", "run.yaml")
        prov_name = lfiles.get("provenance_file", "provenance.json")
        raw_cfg_dict = yaml.safe_load(run_yaml_path.read_text(encoding="utf-8"))

        stage_key = f"__BATCH_STAGE_{uuid.uuid4().hex}__"
        stage_dir = raw_dir.parent / f".stage_{uuid.uuid4().hex}"
        raw_cfg_dict["output_group"] = stage_key
        run_cfg = RunConfig.model_validate(raw_cfg_dict)

        with _HOOK_LOCK:
            _STAGE_ROUTES[stage_key] = stage_dir
        try:
            Engine(run_cfg, run_yaml_path).run()
            if raw_dir.exists():
                shutil.rmtree(raw_dir)
            shutil.move(str(stage_dir / "raw"), str(raw_dir))
            if (stage_dir / "run.log").is_file():
                shutil.move(str(stage_dir / "run.log"), str(raw_dir.parent / "run.log"))
            if (frozen_dir / prov_name).is_file():
                shutil.copy2(frozen_dir / prov_name, raw_dir.parent / prov_name)
        finally:
            with _HOOK_LOCK:
                _STAGE_ROUTES.pop(stage_key, None)
            if stage_dir.exists():
                shutil.rmtree(stage_dir, ignore_errors=True)

        return {"raw_dir": str(raw_dir)}

    def run_stats(
        self,
        raw_dir: Path,
        post_out_dir: Path,
        post_cfg: Dict[str, Any],
        report_cfg: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        eng_opts = post_cfg.get("engine_options") or {}
        opts = {**self.options, **eng_opts}
        repo_path = _ensure_repo(opts)
        from x_band_ngso import config as eng_config
        from x_band_ngso.report import run_post
        from x_band_ngso.settings.post_schema import PostConfig

        base_yaml = Path(str(post_cfg["post_yaml"])).expanduser()
        if not base_yaml.is_absolute():
            base_yaml = (repo_path / "configs" / base_yaml).resolve()
        eff_dict = yaml.safe_load(base_yaml.read_text(encoding="utf-8"))
        eff_dict.update(post_cfg.get("overrides") or {})
        if eff_dict.get("criteria_library") is not None:
            clib = Path(str(eff_dict["criteria_library"]))
            if not clib.is_absolute():
                eff_dict["criteria_library"] = str((repo_path / "configs" / clib).resolve())
        eff_post = PostConfig.model_validate(eff_dict)

        with tempfile.TemporaryDirectory(prefix="xband_post_") as tmp:
            stage_run_dir = Path(tmp) / "run_dir"
            stage_run_dir.mkdir(parents=True)
            (stage_run_dir / "raw").symlink_to(raw_dir.resolve())
            prov_src = raw_dir.parent / "provenance.json"
            if prov_src.is_file():
                (stage_run_dir / "provenance.json").symlink_to(prov_src.resolve())

            eff_yaml = Path(tmp) / "post_effective.yaml"
            eff_yaml.write_text(yaml.safe_dump(eff_dict, sort_keys=False), encoding="utf-8")
            stats_dir = run_post(stage_run_dir, eff_post, eff_yaml)

            if report_cfg.get("enabled"):
                from x_band_ngso.report.docx_report import build_report

                build_report(
                    stage_run_dir,
                    stats_dir=stats_dir,
                    template=eng_config.REPORT_TEMPLATE,
                    org=report_cfg.get("org"),
                )

            post_out_dir.mkdir(parents=True, exist_ok=True)
            for item in stats_dir.iterdir():
                dst = post_out_dir / item.name
                if dst.exists():
                    shutil.rmtree(dst) if dst.is_dir() else dst.unlink()
                shutil.move(str(item), str(dst))

        return _summarize_pair_stats(post_out_dir)


def _write_curve_csv(path: Path, phi: List[float], gain: List[float]) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["phi_deg", "gain_dbi"])
        for p, g in zip(phi, gain):
            w.writerow([f"{float(p):.2f}", f"{float(g):.4f}"])


def _summarize_pair_stats(post_out_dir: Path) -> List[Dict[str, Any]]:
    verdict_path = post_out_dir / "verdict.csv"
    detail_path = post_out_dir / "criteria_detail.csv"
    if not verdict_path.is_file():
        return []
    vdf = pd.read_csv(verdict_path)
    ddf = pd.read_csv(detail_path) if detail_path.is_file() else pd.DataFrame()
    if vdf.empty:
        return []

    rows: List[Dict[str, Any]] = []
    for sid, grp in vdf.groupby("scenario_id", sort=False):
        d_sub = ddf[ddf["scenario_id"] == sid] if not ddf.empty else pd.DataFrame()
        exceed_cols = [c for c in grp.columns if c.endswith("_exceed_percent")]
        max_exc = float(grp[exceed_cols].max().max()) if exceed_cols else float("nan")
        min_margin = float(d_sub["margin_db"].min()) if ("margin_db" in d_sub and not d_sub.empty) else float("nan")

        worst_link = ""
        if not d_sub.empty and "margin_db" in d_sub:
            idx = d_sub["margin_db"].idxmin()
            wr = d_sub.loc[idx]
            worst_link = f"{wr['interf_es']} -> {wr['victim_es']}"
        elif not grp.empty:
            worst_link = f"{grp.iloc[0]['interf_es']} -> {grp.iloc[0]['victim_es']}"

        rows.append({
            "scenario_id": sid,
            "direction": str(grp.iloc[0]["direction"]),
            "criteria_set": str(grp.iloc[0]["criteria_set"]),
            "pass": bool(grp["pass"].all()),
            "max_exceed_percent": max_exc,
            "min_margin_db": min_margin,
            "worst_link": worst_link,
        })
    return rows
