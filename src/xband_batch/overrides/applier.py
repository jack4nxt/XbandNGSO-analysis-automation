"""Apply user overrides to NetworkExtract and resolved antenna curves with provenance."""

from __future__ import annotations

import copy
import fnmatch
import math
from typing import Any, Dict, List, Literal, Tuple

from ..adapters.base import EarthStationDescriptor, NetworkExtract
from ..antenna.models import AntennaCurve, AntennaKey
from ..config.schemas import OverrideItem, OverridesConfig
from ..errors import ConfigError
from .models import OverrideRecord

_DB_FACTOR = 10.0
_HZ_PER_MHZ = 1.0e6
_CARRIER_FIELD_ALIASES = {
    "sat_gmax_dbi": "sat_gain_dbi",
}


def _match(pattern: str, value: str) -> bool:
    return pattern == "*" or fnmatch.fnmatchcase(value.upper(), pattern.upper())


def _apply_carrier(
    net: NetworkExtract,
    item: OverrideItem,
    role: str,
    psd_mode: str,
    curves: Dict[AntennaKey, AntennaCurve] | None,
    records: List[OverrideRecord],
) -> None:
    beam_pat = item.select.beam or "*"
    dir_pat = item.select.direction or "*"

    for c in net.carriers:
        if not _match(beam_pat, c.beam_name):
            continue
        if dir_pat != "*" and c.direction != dir_pat:
            continue

        old_gmax = c.sat_gain_dbi
        old_bw = c.bandwidth_hz
        changed_power = False
        changed_psd = False

        for field_name, val in item.set.items():
            attr = _CARRIER_FIELD_ALIASES.get(field_name, field_name)
            if not hasattr(c, attr):
                raise ConfigError("overrides", f"{item.id}.set.{field_name}", "unknown carrier field")
            old = getattr(c, attr)
            setattr(c, attr, val)
            records.append(
                OverrideRecord(item.id, net.sat_name, role, "carrier", c.carrier_id, field_name, old, val,
                               item.curve_policy, item.reason)
            )
            changed_power |= attr == "power_dbw"
            changed_psd |= attr == "psd_dbw_hz"

        for field_name, delta_val in item.delta.items():
            attr = _CARRIER_FIELD_ALIASES.get(field_name, field_name)
            if not hasattr(c, attr):
                raise ConfigError("overrides", f"{item.id}.delta.{field_name}", "unknown carrier field")
            old = getattr(c, attr)
            if old is None:
                raise ConfigError(
                    "overrides",
                    f"{item.id}.delta.{field_name}",
                    f"cannot apply delta to null declared field on {c.carrier_id}",
                )
            new_val = float(old) + float(delta_val)
            setattr(c, attr, new_val)
            records.append(
                OverrideRecord(item.id, net.sat_name, role, "carrier", c.carrier_id, field_name, old, new_val,
                               item.curve_policy, item.reason)
            )
            changed_power |= attr == "power_dbw"
            changed_psd |= attr == "psd_dbw_hz"

        if "f_low_mhz" in item.set or "f_high_mhz" in item.set:
            if "bandwidth_hz" not in item.set and "bandwidth_hz" not in item.delta:
                new_bw = (c.f_high_mhz - c.f_low_mhz) * _HZ_PER_MHZ
                c.bandwidth_hz = new_bw
                records.append(
                    OverrideRecord(item.id, net.sat_name, role, "carrier", c.carrier_id, "bandwidth_hz",
                                   old_bw, new_bw, None, f"{item.reason} (derived from frequency bounds)")
                )

        if psd_mode == "recompute_missing_or_paired" and c.bandwidth_hz > 0:
            bw_db = _DB_FACTOR * math.log10(c.bandwidth_hz)
            if changed_psd and not changed_power and c.psd_dbw_hz is not None:
                old_p = c.power_dbw
                c.power_dbw = c.psd_dbw_hz + bw_db
                records.append(
                    OverrideRecord(item.id, net.sat_name, role, "carrier", c.carrier_id, "power_dbw",
                                   old_p, c.power_dbw, None, f"{item.reason} (paired with psd_dbw_hz)")
                )
            elif changed_power and not changed_psd and c.power_dbw is not None:
                old_psd = c.psd_dbw_hz
                c.psd_dbw_hz = c.power_dbw - bw_db
                records.append(
                    OverrideRecord(item.id, net.sat_name, role, "carrier", c.carrier_id, "psd_dbw_hz",
                                   old_psd, c.psd_dbw_hz, None, f"{item.reason} (paired with power_dbw)")
                )

        if c.sat_gain_dbi != old_gmax and curves is not None:
            key = (net.sat_name, c.beam_name, c.emi_rcp, "space", "")
            curve = curves.get(key)
            if item.curve_policy == "reject":
                if curve is None or curve.source_name != "user_decision":
                    raise ConfigError(
                        "overrides",
                        f"{item.id}.curve_policy",
                        f"sat_gmax_dbi changed on {key} with curve_policy='reject', "
                        "but no replacement curve was provided via user_decision",
                    )
            elif item.curve_policy == "shift" and curve is not None:
                if old_gmax is None or c.sat_gain_dbi is None:
                    raise ConfigError("overrides", item.id, "cannot shift curve when gmax is null")
                shift_db = float(c.sat_gain_dbi) - float(old_gmax)
                curves[key] = AntennaCurve(
                    phi_deg=list(curve.phi_deg),
                    gain_dbi=[g + shift_db for g in curve.gain_dbi],
                    source_name=curve.source_name,
                    metadata={**curve.metadata, "override_shift_db": shift_db, "override_id": item.id},
                )


def _drop_local_time(obj, item: OverrideItem, net_name: str, role: str, obj_id: str, records: List[OverrideRecord]) -> None:
    """A user-set node longitude must not be replaced by the filed local-time value later."""
    if getattr(obj, "lt_hours", None) is None:
        return
    records.append(
        OverrideRecord(item.id, net_name, role, item.select.kind, obj_id, "lt_hours",
                       obj.lt_hours, None, None, f"{item.reason} (node override disables local-time derivation)")
    )
    obj.lt_hours = None
    obj.lt_type = None


def _apply_generic(
    objs: list,
    id_attr: str,
    pattern: str,
    item: OverrideItem,
    net_name: str,
    role: str,
    records: List[OverrideRecord],
) -> None:
    for obj in objs:
        obj_id = str(getattr(obj, id_attr))
        if not _match(pattern, obj_id):
            continue
        for field_name, val in item.set.items():
            if not hasattr(obj, field_name):
                raise ConfigError("overrides", f"{item.id}.set.{field_name}", f"unknown {item.select.kind} field")
            old = getattr(obj, field_name)
            setattr(obj, field_name, val)
            records.append(
                OverrideRecord(item.id, net_name, role, item.select.kind, obj_id, field_name, old, val,
                               item.curve_policy, item.reason)
            )
        for field_name, delta_val in item.delta.items():
            if not hasattr(obj, field_name):
                raise ConfigError("overrides", f"{item.id}.delta.{field_name}", f"unknown {item.select.kind} field")
            old = getattr(obj, field_name)
            if old is None:
                raise ConfigError("overrides", f"{item.id}.delta.{field_name}", f"null field on {obj_id}")
            new_val = float(old) + float(delta_val)
            setattr(obj, field_name, new_val)
            records.append(
                OverrideRecord(item.id, net_name, role, item.select.kind, obj_id, field_name, old, new_val,
                               item.curve_policy, item.reason)
            )
        if item.select.kind == "orbit" and ("node_deg" in item.set or "node_deg" in item.delta):
            _drop_local_time(obj, item, net_name, role, obj_id, records)


def _apply_stations_list(
    net: NetworkExtract,
    item: OverrideItem,
    role: str,
    records: List[OverrideRecord],
) -> None:
    before = [s.station_id for s in net.stations]
    stations = list(net.stations)
    if "include" in item.set:
        keep = {str(x).upper() for x in item.set["include"]}
        stations = [s for s in stations if s.station_id.upper() in keep]
    if "exclude" in item.set:
        drop = {str(x).upper() for x in item.set["exclude"]}
        stations = [s for s in stations if s.station_id.upper() not in drop]
    if "add" in item.set:
        for raw in item.set["add"]:
            st = EarthStationDescriptor(**raw)
            stations.append(st)
            for c in net.carriers:
                if c.beam_name == st.beam_name and c.emi_rcp == st.emi_rcp and st.station_id not in c.station_ids:
                    c.station_ids.append(st.station_id)
    keep_ids = {s.station_id for s in stations}
    for c in net.carriers:
        c.station_ids = [sid for sid in c.station_ids if sid in keep_ids]
    net.stations = stations
    after = [s.station_id for s in net.stations]
    records.append(
        OverrideRecord(item.id, net.sat_name, role, "stations", "station_list", "stations",
                       before, after, None, item.reason)
    )


def apply_overrides(
    network: NetworkExtract,
    role: Literal["interferer", "victim"],
    cfg: OverridesConfig | None,
    curves: Dict[AntennaKey, AntennaCurve] | None = None,
) -> Tuple[NetworkExtract, Dict[AntennaKey, AntennaCurve], List[OverrideRecord]]:
    """Return deep-copied (NetworkExtract, curves, OverrideRecords) after applying matching items."""
    net = copy.deepcopy(network)
    out_curves = copy.deepcopy(curves) if curves is not None else {}
    curve_arg = out_curves if curves is not None else None  # None: no curve handling (preflight scan)
    records: List[OverrideRecord] = []
    if cfg is None:
        return net, out_curves, records

    for item in cfg.items:
        if not _match(item.target.network, net.sat_name):
            continue
        if item.target.role != "*" and item.target.role != role:
            continue

        kind = item.select.kind
        if kind == "carrier":
            _apply_carrier(net, item, role, cfg.psd_consistency_mode, curve_arg, records)
        elif kind == "orbit":
            _apply_generic(net.planes, "plane_id", item.select.plane_id or "*", item, net.sat_name, role, records)
        elif kind == "earth_station":
            _apply_generic(net.stations, "station_id", item.select.station_id or "*", item, net.sat_name, role, records)
        elif kind == "stations":
            _apply_stations_list(net, item, role, records)

    return net, out_curves, records
