"""Classify matched networks into READY_CO_DIR / DEFERRED_REV_DIR / SKIP_* / NEEDS_REVIEW."""

from __future__ import annotations

import re
from typing import List

from ..adapters.base import CarrierDescriptor, CatalogAdapter, NetworkExtract
from ..config.schemas import BandSpec, FilterRule
from .matcher import MatchOutcome
from .models import CoordinationCategory, CoordinationRow


def _overlaps_band(c: CarrierDescriptor, b: BandSpec) -> bool:
    return min(c.f_high_mhz, b.f_high_mhz) > max(c.f_low_mhz, b.f_low_mhz)


def _carrier_overlaps(a: CarrierDescriptor, b: CarrierDescriptor, band: BandSpec) -> bool:
    lo = max(a.f_low_mhz, b.f_low_mhz, band.f_low_mhz)
    hi = min(a.f_high_mhz, b.f_high_mhz, band.f_high_mhz)
    return hi > lo


def band_text_matches(text: str, bands: List[BandSpec], range_regex: str) -> bool:
    """Return True if text has no parseable frequency ranges OR at least one overlaps a batch band."""
    if not text.strip():
        return True
    matches = re.findall(range_regex, text)
    if not matches:
        return True  # never silently drop unparseable text
    for lo_s, hi_s in matches:
        lo, hi = float(lo_s), float(hi_s)
        if hi <= lo:
            continue
        for b in bands:
            if min(hi, b.f_high_mhz) > max(lo, b.f_low_mhz):
                return True
    return False


def classify_outcome(
    evaluated: NetworkExtract,
    outcome: MatchOutcome,
    catalog: CatalogAdapter,
    bands: List[BandSpec],
    filter_rule: FilterRule,
    country_filter: str | None,
) -> CoordinationRow:
    raw = outcome.entry.raw_entry
    base_kwargs = dict(
        evaluated_network=evaluated.sat_name,
        raw_name=outcome.entry.raw_name,
        normalized_name=outcome.entry.normalized_name,
        source=raw.source,
        listed_chn_stations=raw.chn_stations_text,
    )

    if raw.reason and raw.reason in filter_rule.exclude_reasons:
        return CoordinationRow(
            **base_kwargs,
            matched_name="",
            ntc_id=None,
            adm="",
            orbit_type="UNKNOWN",
            overlap_bands="",
            category="SKIP_FILTERED",
            n_chn_stations=0,
            review_note=f"filtered by reason: {raw.reason}",
        )

    if filter_rule.band_text_prefilter and not band_text_matches(
        raw.frequency_band, bands, filter_rule.band_range_regex
    ):
        return CoordinationRow(
            **base_kwargs,
            matched_name="",
            ntc_id=None,
            adm="",
            orbit_type="UNKNOWN",
            overlap_bands="",
            category="SKIP_FILTERED",
            n_chn_stations=0,
            review_note=f"band text {raw.frequency_band!r} outside configured bands",
        )

    if outcome.status == "skipped":
        return CoordinationRow(
            **base_kwargs,
            matched_name="",
            ntc_id=None,
            adm="",
            orbit_type="UNKNOWN",
            overlap_bands="",
            category="SKIP_FILTERED",
            n_chn_stations=0,
            review_note=outcome.note,
        )

    if outcome.status == "unmatched":
        return CoordinationRow(
            **base_kwargs,
            matched_name="",
            ntc_id=None,
            adm="",
            orbit_type="UNKNOWN",
            overlap_bands="",
            category="UNMATCHED",
            n_chn_stations=0,
            review_note=outcome.note,
        )

    if outcome.status == "ambiguous" or outcome.chosen is None:
        cands = "; ".join(f"{c.sat_name}(ntc={c.ntc_id},adm={c.adm},{c.orbit_type})" for c in outcome.candidates)
        return CoordinationRow(
            **base_kwargs,
            matched_name="",
            ntc_id=None,
            adm="",
            orbit_type="UNKNOWN",
            overlap_bands="",
            category="NEEDS_REVIEW",
            n_chn_stations=0,
            review_note=outcome.note,
            candidate_notices=cands,
        )

    cand = outcome.chosen
    if cand.orbit_type.upper() == "GSO":
        return CoordinationRow(
            **base_kwargs,
            matched_name=cand.sat_name,
            ntc_id=cand.ntc_id,
            adm=cand.adm,
            orbit_type="GSO",
            overlap_bands="",
            category="SKIP_GSO",
            n_chn_stations=0,
            review_note="GSO network skipped in NGSO-NGSO batch",
        )

    band_dicts = [b.model_dump() for b in bands]
    target_extract = catalog.describe_network(cand.ntc_id, band_dicts, country_filter=country_filter)
    n_chn = len(target_extract.stations)

    if filter_rule.require_chn_stations and n_chn == 0:
        return CoordinationRow(
            **base_kwargs,
            matched_name=cand.sat_name,
            ntc_id=cand.ntc_id,
            adm=cand.adm,
            orbit_type=target_extract.orbit_type,
            overlap_bands="",
            category="SKIP_NO_CHN_ES",
            n_chn_stations=0,
            review_note="no domestic (CHN) earth stations in target bands",
        )

    req_srvs = {s.upper() for s in filter_rule.required_services}
    co_bands = set()
    rev_bands = set()
    for b in bands:
        eval_c = [c for c in evaluated.carriers if _overlaps_band(c, b)]
        targ_c = [c for c in target_extract.carriers if _overlaps_band(c, b)]
        for ca in eval_c:
            for cb in targ_c:
                if not _carrier_overlaps(ca, cb, b):
                    continue
                if req_srvs:
                    ca_srvs = {s.upper() for s in ca.services}
                    cb_srvs = {s.upper() for s in cb.services}
                    if not (ca_srvs & req_srvs) or not (cb_srvs & req_srvs):
                        continue
                if ca.direction == cb.direction and ca.direction in b.directions:
                    co_bands.add(b.name)
                elif ca.direction != cb.direction:
                    rev_bands.add(b.name)

    if co_bands:
        category: CoordinationCategory = "READY_CO_DIR"
        overlap_str = ";".join(sorted(co_bands))
        note = outcome.note
    elif rev_bands:
        category = "DEFERRED_REV_DIR"
        overlap_str = ";".join(sorted(rev_bands))
        note = "co-frequency reverse-direction overlap (deferred to EESS-FSS pipeline)"
    else:
        category = "SKIP_FILTERED"
        overlap_str = ""
        note = "no overlapping carriers in configured bands"

    return CoordinationRow(
        **base_kwargs,
        matched_name=cand.sat_name,
        ntc_id=cand.ntc_id,
        adm=cand.adm,
        orbit_type=target_extract.orbit_type,
        overlap_bands=overlap_str,
        category=category,
        n_chn_stations=n_chn,
        review_note=note,
    )
