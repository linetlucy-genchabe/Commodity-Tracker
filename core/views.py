"""
All views for the Commodity Tracker, plus the query-helper functions that
build dashboard context and the tiny upload form. Kept in one file — see
README "Project layout".
"""

import calendar
import json

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required, user_passes_test
from django.core.paginator import Paginator
from django.core.serializers.json import DjangoJSONEncoder
from django.db.models import Avg, Count, Max, Sum
from django.shortcuts import get_object_or_404, redirect, render

from .models import (
    CHPArea,
    CHPCommodity,
    CHPCommodityRecord,
    CHPCommodityStockStatus,
    CHPCommodityUpload,
    Commodity,
    CommunityHealthUnit,
    County,
    Facility,
    FormDefinition,
    MOH748Record,
    MOH748Upload,
    Role,
    Status,
    SubCounty,
    Threshold,
    Ward,
)
from .parsing import parse_chp_commodity_workbook, parse_moh748_workbook

PAGE_SIZE = 25
TREND_MONTHS = 12


# ---------------------------------------------------------------------------
# Query helpers (formerly dashboard/services.py)
# ---------------------------------------------------------------------------


def period_display(period):
    if not period or "-" not in period:
        return period
    year, month = period.split("-", 1)
    try:
        from datetime import date

        return date(int(year), int(month), 1).strftime("%B %Y")
    except (ValueError, TypeError):
        return period


def available_periods():
    return list(
        MOH748Record.objects.exclude(period="")
        .order_by("-period")
        .values_list("period", flat=True)
        .distinct()
    )


def scoped_facilities(user):
    return user.facility_queryset()


def apply_geo_filters(queryset, *, county_id=None, sub_county_id=None, ward_id=None, facility_field=None):
    """
    Filter a queryset by county/sub_county/ward.

    Pass facility_field=None (default) when queryset is a Facility queryset
    itself; pass facility_field="facility" (or another FK name) when
    filtering a related queryset — e.g. MOH748Record — that reaches Facility
    through that foreign key.
    """
    prefix = "" if facility_field is None else f"{facility_field}__"

    if county_id:
        queryset = queryset.filter(**{f"{prefix}ward__sub_county__county_id": county_id})
    if sub_county_id:
        queryset = queryset.filter(**{f"{prefix}ward__sub_county_id": sub_county_id})
    if ward_id:
        queryset = queryset.filter(**{f"{prefix}ward_id": ward_id})
    return queryset


def filter_options(user, *, county_id=None, sub_county_id=None, ward_id=None):
    """Cascading dropdown options, scoped to what this user is allowed to see."""
    facilities = scoped_facilities(user)

    county_ids = facilities.values_list("ward__sub_county__county_id", flat=True).distinct()
    counties = list(County.objects.filter(id__in=county_ids).order_by("name").values("id", "name"))

    sc_qs = facilities
    if county_id:
        sc_qs = sc_qs.filter(ward__sub_county__county_id=county_id)
    sub_county_ids = sc_qs.values_list("ward__sub_county_id", flat=True).distinct()
    sub_counties = list(SubCounty.objects.filter(id__in=sub_county_ids).order_by("name").values("id", "name"))

    w_qs = facilities
    if sub_county_id:
        w_qs = w_qs.filter(ward__sub_county_id=sub_county_id)
    elif county_id:
        w_qs = w_qs.filter(ward__sub_county__county_id=county_id)
    ward_ids = w_qs.values_list("ward_id", flat=True).distinct()
    wards = list(Ward.objects.filter(id__in=ward_ids).order_by("name").values("id", "name"))

    f_qs = facilities
    if ward_id:
        f_qs = f_qs.filter(ward_id=ward_id)
    elif sub_county_id:
        f_qs = f_qs.filter(ward__sub_county_id=sub_county_id)
    elif county_id:
        f_qs = f_qs.filter(ward__sub_county__county_id=county_id)
    facility_list = list(f_qs.order_by("name").values("id", "name"))

    return {
        "counties": counties,
        "sub_counties": sub_counties,
        "wards": wards,
        "facilities": facility_list,
    }


def build_kpis(records_qs, facilities_qs):
    total_in_scope = facilities_qs.count()
    reporting = records_qs.values("facility_id").distinct().count()

    with_stockout = records_qs.filter(days_out_of_stock__gt=0).values("facility_id").distinct().count()
    with_stockout_pct = round((with_stockout / reporting) * 100, 1) if reporting else 0

    avg_days = records_qs.aggregate(v=Avg("days_out_of_stock"))["v"]
    avg_days = round(float(avg_days), 1) if avg_days is not None else 0

    near_expiry = records_qs.aggregate(v=Sum("near_expiry_6mo"))["v"] or 0

    # Same share-of-reporting-units grading as the CHP dashboard (see
    # build_chp_kpis) so a unit-card's colored border/stockout-% pill means
    # the same thing on both dashboards: red is genuinely bad, not just
    # "at least one facility has any stockout at all".
    if not reporting:
        severity = "none"
    elif with_stockout_pct >= 50:
        severity = "red"
    elif with_stockout_pct >= 20:
        severity = "amber"
    else:
        severity = "green"

    return {
        "total_in_scope": total_in_scope,
        "reporting": reporting,
        "with_stockout": with_stockout,
        "with_stockout_pct": with_stockout_pct,
        "avg_days": avg_days,
        "near_expiry": near_expiry,
        "severity": severity,
    }


def _delta_badge(current, previous, *, higher_is_better):
    """
    A "vs last period" comparison badge for one KPI. Returns None when
    there's no earlier period to compare against (no badge renders rather
    than a misleading 0%). `higher_is_better` decides whether an increase
    is good news (more facilities reporting) or bad news (more stockouts,
    more near-expiry stock, a higher average days-out-of-stock).
    """
    if previous is None:
        return None
    if current == previous:
        return {"css_class": "flat", "arrow": "·", "text": "No change"}
    went_up = current > previous
    good = went_up == higher_is_better
    arrow = "↑" if went_up else "↓"
    if previous:
        pct = abs(round(((current - previous) / previous) * 100, 1))
        text = f"{pct:g}%"
    else:
        text = "new"
    return {"css_class": "good" if good else "bad", "arrow": arrow, "text": text}


def build_kpi_deltas(current_kpis, previous_kpis):
    """"vs last period" badges for the KPI row, or None entirely when there's no earlier period in scope to compare against."""
    if previous_kpis is None:
        return None
    return {
        "reporting": _delta_badge(current_kpis["reporting"], previous_kpis["reporting"], higher_is_better=True),
        "with_stockout": _delta_badge(current_kpis["with_stockout"], previous_kpis["with_stockout"], higher_is_better=False),
        "avg_days": _delta_badge(current_kpis["avg_days"], previous_kpis["avg_days"], higher_is_better=False),
        "near_expiry": _delta_badge(current_kpis["near_expiry"], previous_kpis["near_expiry"], higher_is_better=False),
    }


# Fixed per-commodity palette (same order as Commodity.choices) so the
# donut chart, its legend dots, and the bar chart all agree on one color
# per commodity across every render of the page.
COMMODITY_COLORS = ["#2F9BD6", "#F0793F", "#6C4F9E", "#3F9A56", "#E5574B"]

# Same palette, keyed by enum member so any table row (balances, facility
# drilldown, the CHP-generated 748 preview) can look up "my" color without
# caring what order it happens to be rendered in — a commodity is the same
# color everywhere in the app, not just in the charts.
COMMODITY_COLOR_MAP = dict(zip(Commodity, COMMODITY_COLORS))


def build_commodity_stockout_distribution(records_qs):
    """
    Stockout counts by commodity, in scope, for the donut chart. Every
    commodity is always included (even at 0) so the legend never silently
    drops one just because it had no stockouts this period.
    """
    counts = {c.value: 0 for c in Commodity}
    for row in records_qs.filter(days_out_of_stock__gt=0).values("commodity").annotate(n=Count("id")):
        counts[row["commodity"]] = row["n"]
    total = sum(counts.values())
    return {
        "labels": [c.label for c in Commodity],
        "counts": [counts[c.value] for c in Commodity],
        "colors": COMMODITY_COLORS,
        "total": total,
        "breakdown": [
            {
                "label": c.label,
                "count": counts[c.value],
                "pct": round(counts[c.value] / total * 100, 1) if total else 0,
                "color": COMMODITY_COLORS[i],
            }
            for i, c in enumerate(Commodity)
        ],
    }


_SEVERITY_RANK = {Status.RED: 3, Status.AMBER: 2, Status.GREEN: 1}


def build_heatmap(records_qs, *, page_number=1):
    """
    Facility-level heatmap. Every facility with a record in records_qs gets
    a row here — no facility is ever filtered out or reclassified based on
    a guess about its name. (An earlier version tried to detect and hide
    "ward-level aggregate" rows whose name happened to match their ward;
    that heuristic was too broad and was silently hiding real facilities
    from the list — e.g. a sub-county that genuinely has a dozen facilities
    showing only one. Never guess away real data again: if a row needs
    special handling later, it must be additive — shown, just marked — not
    a filter that can make a facility disappear.)

    Most facilities only have a handful of reported metrics per period, so
    a plain alphabetical list buries the few rows that actually need
    attention under dozens of all-blank ones — sort the worst stockouts to
    the top instead, so the table tells a story instead of reading as a
    wall of "not reported" dashes.
    """
    facility_ids = list(records_qs.values_list("facility_id", flat=True).distinct())
    facilities = list(
        Facility.objects.filter(id__in=facility_ids).select_related("ward__sub_county__county")
    )

    by_facility_commodity = {(r.facility_id, r.commodity): r for r in records_qs}

    rows = []
    for facility in facilities:
        cells = []
        severity = 0
        for commodity in Commodity:
            record = by_facility_commodity.get((facility.id, commodity))
            if record is None or record.days_out_of_stock is None:
                cells.append(None)
                continue
            if record.days_out_of_stock <= 0:
                status = Status.GREEN
            elif record.days_out_of_stock <= 6:
                status = Status.AMBER
            else:
                status = Status.RED
            severity = max(severity, _SEVERITY_RANK[status])
            cells.append({"value": record.days_out_of_stock, "status": status})
        reporting = sum(1 for cell in cells if cell is not None)
        rows.append({"facility": facility, "cells": cells, "_severity": severity, "reporting": reporting})

    rows.sort(key=lambda r: (-r["_severity"], r["facility"].name))
    for row in rows:
        del row["_severity"]

    paginator = Paginator(rows, PAGE_SIZE)
    page_obj = paginator.get_page(page_number)

    return {
        "rows": page_obj.object_list,
        "commodities": [(c.value, c.label, COMMODITY_COLOR_MAP[c]) for c in Commodity],
        "page": page_obj,
    }


def build_geo_summary(records_qs, facilities_qs, *, group_by, nested=False):
    """
    Aggregate KPIs one level down the geography hierarchy — one row per
    county or per sub-county. Used for the county/sub-county summary screens
    that sit above the facility-level heatmap.

    nested=True (only meaningful when group_by="county") also attaches each
    county's own sub-county breakdown as row["sub_rows"], so the sub-counties
    are visible immediately rather than behind an extra click.
    """
    if group_by == "county":
        model, fk, id_field = County, "ward__sub_county__county", "ward__sub_county__county_id"
    elif group_by == "ward":
        model, fk, id_field = Ward, "ward", "ward_id"
    else:
        model, fk, id_field = SubCounty, "ward__sub_county", "ward__sub_county_id"

    unit_ids = facilities_qs.values_list(id_field, flat=True).distinct()
    units = model.objects.filter(id__in=unit_ids).order_by("name")

    rows = []
    for unit in units:
        unit_facilities = facilities_qs.filter(**{fk: unit})
        unit_records = records_qs.filter(facility__in=unit_facilities)
        row = {"unit": unit, "kpis": build_kpis(unit_records, unit_facilities)}
        if nested and group_by == "county":
            row["sub_rows"] = build_geo_summary(unit_records, unit_facilities, group_by="sub_county")
        rows.append(row)

    # Worst first: units with active stockouts float to the top (most
    # stockouts, then highest avg. days out), the same "worst first" logic
    # already used for the facility heatmap — so this table leads with what
    # needs attention instead of an alphabetical list that happens to bury
    # the one county or sub-county with a real problem in the middle.
    rows.sort(key=lambda r: (-r["kpis"]["with_stockout"], -r["kpis"]["avg_days"], r["unit"].name))
    return rows


def build_balance_summary(records_qs):
    """
    Per-commodity stock balances (beginning balance, received, dispensed,
    physical count, near-expiry, avg. days out of stock) totalled across
    every facility currently in scope — the same fields the facility
    drilldown shows for one facility, rolled up to whatever county or
    sub-county is currently selected.

    A commodity with no records at all in scope gets None for every field
    (rendered as "not reported"); a commodity that genuinely summed to zero
    (e.g. 0 units received) gets 0, kept distinct from "no data".
    """
    rows = []
    for commodity in Commodity:
        agg = records_qs.filter(commodity=commodity).aggregate(
            beginning_balance=Sum("beginning_balance"),
            quantity_received=Sum("quantity_received"),
            total_dispensed=Sum("total_dispensed"),
            physical_count=Sum("physical_count"),
            near_expiry_6mo=Sum("near_expiry_6mo"),
            avg_days_out_of_stock=Avg("days_out_of_stock"),
        )
        avg_days = agg["avg_days_out_of_stock"]
        rows.append(
            {
                "commodity": commodity,
                "beginning_balance": agg["beginning_balance"],
                "quantity_received": agg["quantity_received"],
                "total_dispensed": agg["total_dispensed"],
                "physical_count": agg["physical_count"],
                "near_expiry_6mo": agg["near_expiry_6mo"],
                "avg_days_out_of_stock": round(float(avg_days), 1) if avg_days is not None else None,
                "color": COMMODITY_COLOR_MAP[commodity],
            }
        )
    return rows


def top_stockout_facilities(records_qs, limit=5):
    return list(
        records_qs.filter(days_out_of_stock__gt=0)
        .values("facility__name", "facility__ward__sub_county__name")
        .annotate(stockout_count=Count("id"))
        .order_by("-stockout_count")[:limit]
    )


def trend_series(records_qs, months=TREND_MONTHS):
    periods = list(
        records_qs.exclude(period="").order_by("-period").values_list("period", flat=True).distinct()[:months]
    )
    periods.reverse()

    series = {commodity.value: [] for commodity in Commodity}
    for period in periods:
        for commodity in Commodity:
            avg = records_qs.filter(period=period, commodity=commodity).aggregate(avg=Avg("days_out_of_stock"))["avg"]
            series[commodity.value].append(round(float(avg), 1) if avg is not None else None)

    return {"periods": periods, "series": series}


# ---------------------------------------------------------------------------
# CHP Commodity Stock Flow — query helpers
#
# A separate module from MOH 748: different reporting unit (CHP Area, not
# Facility), different commodity set, its own upload/record tables. The
# drill-down shape mirrors 748's County > Sub-county > choose > level > level
# exactly, just one geography branch over (County > Sub-county > CHU > CHP
# Area instead of County > Sub-county > Ward > Facility).
# ---------------------------------------------------------------------------

CHP_SEVERITY_RANK = {
    CHPCommodityStockStatus.STOCKOUT: 3,
    CHPCommodityStockStatus.LOW_STOCK: 2,
    CHPCommodityStockStatus.ADEQUATE: 1,
}

# eCHIS's own Stockout/Low stock/Adequate classification, mapped onto the
# same GREEN/AMBER/RED pill styling the rest of the dashboard already uses
# — one consistent visual language, even though this status comes from
# eCHIS rather than from a threshold set here.
CHP_STATUS_CSS = {
    CHPCommodityStockStatus.STOCKOUT: "RED",
    CHPCommodityStockStatus.LOW_STOCK: "AMBER",
    CHPCommodityStockStatus.ADEQUATE: "GREEN",
}
CHP_STATUS_SHORT = {
    CHPCommodityStockStatus.STOCKOUT: "Out",
    CHPCommodityStockStatus.LOW_STOCK: "Low",
    CHPCommodityStockStatus.ADEQUATE: "OK",
}

# One color per CHP commodity, reused everywhere a commodity name is shown
# (Commodity Balances, the generated 748 preview). The four AL pack sizes
# reuse MOH 748's own colors for those exact commodities (COMMODITY_COLOR_MAP)
# so AL 12s, say, is the same purple on both the CHP and the 748 dashboard;
# the four CHP-only commodities (no 748 equivalent) get their own colors.
CHP_COMMODITY_COLOR_MAP = {
    CHPCommodity.AL_6: COMMODITY_COLOR_MAP[Commodity.AL_6],
    CHPCommodity.AL_12: COMMODITY_COLOR_MAP[Commodity.AL_12],
    CHPCommodity.AL_18: COMMODITY_COLOR_MAP[Commodity.AL_18],
    CHPCommodity.AL_24: COMMODITY_COLOR_MAP[Commodity.AL_24],
    CHPCommodity.RDTS: "#2F9BD6",
    CHPCommodity.AMOXICILLIN_DT250: "#1F9E93",
    CHPCommodity.ORS_ZINC: "#D64C8C",
    CHPCommodity.ORS_SACHETS: "#B9860F",
    CHPCommodity.ZINC_SULPHATE: "#7A5CC7",
    CHPCommodity.PARACETAMOL: "#4C7EF0",
    CHPCommodity.GLUCOMETER_STRIPS: "#E08A1E",
    CHPCommodity.GLOVES: "#5FB3A3",
    CHPCommodity.DISPENSING_ENVELOPES: "#B85C7A",
}


def chp_available_periods():
    return list(
        CHPCommodityRecord.objects.exclude(period="")
        .order_by("-period")
        .values_list("period", flat=True)
        .distinct()
    )


def chp_scoped_areas(user):
    """
    CHP Areas this user is allowed to see. Kept independent of
    facility_queryset() for now — CommunityHealthUnit.facility is still
    blank for every CHU until Lynne supplies the CHU-to-facility mapping,
    so there's no real link to scope through yet for a Facility In-charge.
    Fails closed, same as facility_queryset(): a scoped role with no
    geography assigned sees nothing rather than everything.
    """
    if not user.is_scoped:
        return CHPArea.objects.all()

    if user.role == Role.FACILITY_INCHARGE:
        if user.facility_id:
            return CHPArea.objects.filter(community_health_unit__facility_id=user.facility_id)
        return CHPArea.objects.none()

    if user.role == Role.SCHMT:
        if user.sub_county_id:
            return CHPArea.objects.filter(community_health_unit__sub_county_id=user.sub_county_id)
        return CHPArea.objects.none()

    if user.role == Role.CHMT:
        if user.county_id:
            # county is always set, even on an area with unresolved
            # CHU/sub-county — a CHMT's county-wide scope is broad enough
            # that those rows still belong to them, not hidden.
            return CHPArea.objects.filter(county_id=user.county_id)
        return CHPArea.objects.none()

    return CHPArea.objects.none()


def apply_chp_geo_filters(queryset, *, county_id=None, sub_county_id=None, chu_id=None, area_field=None):
    prefix = "" if area_field is None else f"{area_field}__"

    if county_id:
        queryset = queryset.filter(**{f"{prefix}county_id": county_id})
    if sub_county_id:
        queryset = queryset.filter(**{f"{prefix}community_health_unit__sub_county_id": sub_county_id})
    if chu_id:
        queryset = queryset.filter(**{f"{prefix}community_health_unit_id": chu_id})
    return queryset


def chp_filter_options(user, *, county_id=None, sub_county_id=None, chu_id=None):
    areas = chp_scoped_areas(user)

    county_ids = areas.values_list("county_id", flat=True).distinct()
    counties = list(County.objects.filter(id__in=county_ids).order_by("name").values("id", "name"))

    resolved = areas.filter(community_health_unit__isnull=False)

    sc_qs = resolved
    if county_id:
        sc_qs = sc_qs.filter(county_id=county_id)
    sub_county_ids = sc_qs.values_list("community_health_unit__sub_county_id", flat=True).distinct()
    sub_counties = list(SubCounty.objects.filter(id__in=sub_county_ids).order_by("name").values("id", "name"))

    chu_qs = resolved
    if sub_county_id:
        chu_qs = chu_qs.filter(community_health_unit__sub_county_id=sub_county_id)
    elif county_id:
        chu_qs = chu_qs.filter(county_id=county_id)
    chu_ids = chu_qs.values_list("community_health_unit_id", flat=True).distinct()
    chus = list(CommunityHealthUnit.objects.filter(id__in=chu_ids).order_by("name").values("id", "name"))

    area_qs = areas
    if chu_id:
        area_qs = area_qs.filter(community_health_unit_id=chu_id)
    elif sub_county_id:
        area_qs = area_qs.filter(community_health_unit__sub_county_id=sub_county_id)
    elif county_id:
        area_qs = area_qs.filter(county_id=county_id)
    area_list = list(area_qs.order_by("name").values("id", "name"))

    return {"counties": counties, "sub_counties": sub_counties, "chus": chus, "areas": area_list}


def weeks_of_stock_severity(weeks):
    """
    Lynne's 4-band read on an average weeks-of-stock figure, used wherever
    a weeks-of-stock number gets a colored pill on the CHP dashboard: both
    ends are flagged red (under 2 weeks is stockout/low stock, over 6 weeks
    is overstocked), and only the middle two bands -- 2-4 weeks (amber),
    4-6 weeks (green) -- read as a healthy supply position. Replaces the
    earlier, coarser 3-band version (<1 red / <=2 amber / else green).
    """
    if weeks is None:
        return None
    weeks = float(weeks)
    if weeks < 2:
        return "RED"
    if weeks < 4:
        return "AMBER"
    if weeks <= 6:
        return "GREEN"
    return "RED"


def build_chp_kpis(records_qs, areas_qs):
    total_in_scope = areas_qs.count()
    reporting = records_qs.values("chp_area_id").distinct().count()

    with_stockout = (
        records_qs.filter(stock_status=CHPCommodityStockStatus.STOCKOUT)
        .values("chp_area_id")
        .distinct()
        .count()
    )
    with_stockout_pct = round((with_stockout / reporting) * 100, 1) if reporting else 0

    with_low_stock = (
        records_qs.filter(stock_status=CHPCommodityStockStatus.LOW_STOCK)
        .values("chp_area_id")
        .distinct()
        .count()
    )
    with_low_stock_pct = round((with_low_stock / reporting) * 100, 1) if reporting else 0

    avg_weeks = records_qs.aggregate(v=Avg("weeks_of_stock"))["v"]
    avg_weeks = round(float(avg_weeks), 1) if avg_weeks is not None else None
    avg_weeks_status = weeks_of_stock_severity(avg_weeks)

    unresolved_areas = areas_qs.filter(community_health_unit__isnull=True).count()

    # Each area tracks 8 commodities, and Lynne's real eCHIS data has a
    # genuinely high per-record stockout rate — a binary "any commodity
    # out at all" trigger (what this used to use) paints almost every row
    # red regardless of how bad it actually is, since at least one of 8
    # commodities being out is the common case, not the exception. Grading
    # by the *share* of reporting areas affected keeps red for what's
    # actually severe and gives amber somewhere real to mean something.
    if not reporting:
        severity = "none"
    elif with_stockout_pct >= 50:
        severity = "red"
    elif with_stockout_pct >= 20:
        severity = "amber"
    else:
        severity = "green"

    return {
        "total_in_scope": total_in_scope,
        "reporting": reporting,
        "with_stockout": with_stockout,
        "with_stockout_pct": with_stockout_pct,
        "with_low_stock": with_low_stock,
        "with_low_stock_pct": with_low_stock_pct,
        "avg_weeks": avg_weeks,
        "avg_weeks_status": avg_weeks_status,
        "avg_weeks_overstocked": avg_weeks is not None and avg_weeks > 6,
        "unresolved_areas": unresolved_areas,
        "severity": severity,
    }


def chp_last_received_by_area(area_ids):
    """
    Most recent period (any commodity) each CHP area recorded a nonzero
    Quantity Received, looked up across the area's ENTIRE history -- not
    scoped to whatever period is currently selected, since "when did this
    area last get supplied" is a question about its whole record, not just
    the one month in view. Month-level only: the source data has no
    day-level receipt date, only which reporting month a delivery landed in.
    """
    rows = (
        CHPCommodityRecord.objects.filter(chp_area_id__in=area_ids, quantity_received__gt=0)
        .values("chp_area_id")
        .annotate(last_received_period=Max("period"))
    )
    return {row["chp_area_id"]: row["last_received_period"] for row in rows}


def build_chp_heatmap(records_qs, *, page_number=1):
    """
    CHP-area-level heatmap, one row per area with a record in records_qs —
    same "never hide a row on a guess" rule as MOH 748's build_heatmap().
    Cells are colored using the stock status eCHIS already computed
    (Stockout/Low stock/Adequate) rather than a threshold Lynne would set
    herself, but the cell TEXT is the actual stock-on-hand balance (not
    just the status word) — same "show the real number, not just plastic
    coloring" pattern MOH 748's own Facility Stock Status heatmap uses.
    """
    area_ids = list(records_qs.values_list("chp_area_id", flat=True).distinct())
    areas = list(CHPArea.objects.filter(id__in=area_ids).select_related("community_health_unit__sub_county__county"))

    by_area_commodity = {(r.chp_area_id, r.commodity): r for r in records_qs}
    last_received_by_area = chp_last_received_by_area(area_ids)

    rows = []
    for area in areas:
        cells = []
        severity = 0
        for commodity in CHPCommodity:
            record = by_area_commodity.get((area.id, commodity))
            if record is None or not record.stock_status:
                cells.append(None)
                continue
            severity = max(severity, CHP_SEVERITY_RANK.get(record.stock_status, 0))
            cells.append(
                {
                    "label": CHP_STATUS_SHORT.get(record.stock_status, record.stock_status),
                    "full_label": record.stock_status,
                    "css": CHP_STATUS_CSS.get(record.stock_status, ""),
                    "weeks_of_stock": record.weeks_of_stock,
                    "stock_on_hand": record.stock_on_hand,
                }
            )
        reporting = sum(1 for cell in cells if cell is not None)
        last_received_period = last_received_by_area.get(area.id)
        rows.append(
            {
                "area": area,
                "cells": cells,
                "_severity": severity,
                "reporting": reporting,
                "last_received_period": last_received_period,
                "last_received_display": period_display(last_received_period) if last_received_period else None,
            }
        )

    rows.sort(key=lambda r: (-r["_severity"], (r["area"].name or r["area"].external_id)))
    for row in rows:
        del row["_severity"]

    paginator = Paginator(rows, PAGE_SIZE)
    page_obj = paginator.get_page(page_number)

    return {
        "rows": page_obj.object_list,
        "commodities": [(c.value, c.label, CHP_COMMODITY_COLOR_MAP[c]) for c in CHPCommodity],
        "page": page_obj,
    }


def build_chp_geo_summary(records_qs, areas_qs, *, group_by):
    """
    Aggregate KPIs one level down the community geography — one row per
    county, sub-county, or CHU. An area whose CHU/sub-county couldn't be
    resolved is never silently dropped from this table: it's surfaced as an
    extra "Unresolved geography" row instead (see below), same principle as
    build_heatmap()'s "never guess away real data".
    """
    if group_by == "county":
        model, fk, id_field = County, "county", "county_id"
    elif group_by == "chu":
        model, fk, id_field = CommunityHealthUnit, "community_health_unit", "community_health_unit_id"
    else:  # sub_county
        model, fk, id_field = SubCounty, "community_health_unit__sub_county", "community_health_unit__sub_county_id"

    unit_ids = areas_qs.exclude(**{f"{id_field}__isnull": True}).values_list(id_field, flat=True).distinct()
    units = model.objects.filter(id__in=unit_ids).order_by("name")

    rows = []
    for unit in units:
        unit_areas = areas_qs.filter(**{fk: unit})
        unit_records = records_qs.filter(chp_area__in=unit_areas)
        rows.append({"unit": unit, "unresolved": False, "kpis": build_chp_kpis(unit_records, unit_areas)})

    rows.sort(key=lambda r: (-r["kpis"]["with_stockout"], r["unit"].name))

    # Areas with no resolved CHU (and, at the sub-county level, no resolved
    # sub-county either) never disappear — they get one extra summary row
    # instead of being left out of the table entirely.
    if group_by in ("sub_county", "chu"):
        unresolved_areas = areas_qs.filter(community_health_unit__isnull=True)
        if unresolved_areas.exists():
            unresolved_records = records_qs.filter(chp_area__in=unresolved_areas)
            rows.append(
                {
                    "unit": None,
                    "unresolved": True,
                    "kpis": build_chp_kpis(unresolved_records, unresolved_areas),
                }
            )

    return rows


def build_chp_balance_summary(records_qs):
    """
    Per-commodity stock-flow totals (beginning balance, received, dispensed,
    ending balance, physical count, stock on hand, avg. weeks of stock)
    across whatever CHP-area scope is currently in view — same role as MOH
    748's build_balance_summary(), and shown the same way: visible at every
    drill-down level, narrowing as the geography filter narrows.

    A commodity with no records at all in scope gets None for every field
    (rendered as "not reported"); a commodity that genuinely summed to zero
    gets 0, kept distinct from "no data" — same rule as MOH 748.

    A handful of eCHIS rows carry a Quantity Dispensed value eCHIS itself
    has flagged as implausible (service_qty_review_flag is set — eCHIS's own
    "raw total > 10,000 units" review flag; one real row in Lynne's data was
    1,261,123,456,852 units, which alone would have swamped every other
    total by six orders of magnitude). Those rows' OTHER fields (beginning
    balance, received, physical count, stock on hand) look normal and stay
    in every sum; only Quantity Dispensed is excluded for the flagged rows,
    and the number excluded is returned so the page can say so rather than
    quietly dropping data.
    """
    rows = []
    for commodity in CHPCommodity:
        commodity_qs = records_qs.filter(commodity=commodity)
        agg = commodity_qs.aggregate(
            beginning_balance=Sum("beginning_balance"),
            quantity_received=Sum("quantity_received"),
            ending_balance=Sum("ending_balance"),
            physical_count=Sum("physical_count"),
            stock_on_hand=Sum("stock_on_hand"),
            avg_weeks_of_stock=Avg("weeks_of_stock"),
        )
        clean_qs = commodity_qs.filter(service_qty_review_flag="")
        dispensed_agg = clean_qs.aggregate(quantity_dispensed=Sum("quantity_dispensed"))
        flagged_count = commodity_qs.exclude(service_qty_review_flag="").count()

        avg_weeks = agg["avg_weeks_of_stock"]
        avg_weeks_rounded = round(float(avg_weeks), 1) if avg_weeks is not None else None
        rows.append(
            {
                "commodity": commodity,
                "beginning_balance": agg["beginning_balance"],
                "quantity_received": agg["quantity_received"],
                "quantity_dispensed": dispensed_agg["quantity_dispensed"],
                "ending_balance": agg["ending_balance"],
                "physical_count": agg["physical_count"],
                "stock_on_hand": agg["stock_on_hand"],
                "avg_weeks_of_stock": avg_weeks_rounded,
                "weeks_status": weeks_of_stock_severity(avg_weeks_rounded),
                "weeks_overstocked": avg_weeks_rounded is not None and avg_weeks_rounded > 6,
                "flagged_rows_excluded": flagged_count,
                "color": CHP_COMMODITY_COLOR_MAP[commodity],
            }
        )
    return rows


# ---------------------------------------------------------------------------
# Generated MOH 748 preview — from CHP Commodity Stock Flow data
#
# NOT written to MOH748Record — Lynne was explicit that eCHIS-derived
# numbers should never be stored as if they were a real facility 748
# submission. This is a preview of what a facility's 748 would look like if
# generated from whatever CHP-area scope is currently selected, built now so
# the conversion rule can be checked against real data before the
# CHU-to-facility mapping arrives and it becomes possible to actually attach
# one of these to a facility.
#
# Only the four AL pack sizes have any eCHIS equivalent. LLINs and the
# CHP-only commodities (RDTs, Amoxicillin, Zinc/ORS, ORS Sachets) simply
# have no 748 slot — LLINs is shown as "not available from eCHIS" rather
# than silently missing from a "748" table; the CHP-only four don't appear
# at all, because 748 itself doesn't track them.
# ---------------------------------------------------------------------------

CHP_TO_MOH748_COMMODITY = {
    CHPCommodity.AL_6: Commodity.AL_6,
    CHPCommodity.AL_12: Commodity.AL_12,
    CHPCommodity.AL_18: Commodity.AL_18,
    CHPCommodity.AL_24: Commodity.AL_24,
}


def estimate_days_out_of_stock(stock_status, period):
    """
    eCHIS reports a Stockout/Low stock/Adequate snapshot, not an exact day
    count, but MOH 748 wants an exact day count — Lynne's explicit choice
    (over leaving the field blank) was to estimate it. This reuses the same
    draft "Days Out of Stock" threshold already used to color-code MOH 748
    itself: Adequate -> 0, Low stock -> the threshold's own amber ceiling
    (the edge of "in trouble" rather than a made-up middle value), Stockout
    -> treated as out for the whole reporting month. This is an ESTIMATE,
    not a measurement — always label it as such wherever it's shown.
    """
    if not stock_status:
        return None
    if stock_status == CHPCommodityStockStatus.ADEQUATE:
        return 0

    if stock_status == CHPCommodityStockStatus.LOW_STOCK:
        threshold = Threshold.objects.filter(metric_key="days_out_of_stock").order_by("-form_id").first()
        return int(threshold.amber_max) if threshold else 6

    if stock_status == CHPCommodityStockStatus.STOCKOUT:
        if period and "-" in period:
            try:
                year, month = (int(p) for p in period.split("-", 1))
                return calendar.monthrange(year, month)[1]
            except (ValueError, TypeError):
                pass
        return 30

    return None


def build_chp_moh748_preview(records_qs):
    """
    A 748-shaped preview built from whatever CHP-area scope is currently in
    view. estimated_days_out_of_stock is the average of the per-record
    estimate across every CHP area/period in scope — same "average across
    what's in view" idea MOH 748's own dashboard already uses for
    facilities, just applied to the estimated values instead of measured
    ones.

    Same review-flag handling as build_chp_balance_summary(): rows eCHIS
    itself flagged as an implausible Quantity Dispensed value are excluded
    from total_dispensed only, with the excluded count returned so the
    preview can say so.
    """
    rows = []
    for chp_commodity, moh_commodity in CHP_TO_MOH748_COMMODITY.items():
        commodity_qs = records_qs.filter(commodity=chp_commodity)
        if not commodity_qs.exists():
            rows.append(
                {
                    "commodity": moh_commodity,
                    "available": False,
                    "beginning_balance": None,
                    "quantity_received": None,
                    "total_dispensed": None,
                    "physical_count": None,
                    "estimated_days_out_of_stock": None,
                    "flagged_rows_excluded": 0,
                    "color": COMMODITY_COLOR_MAP[moh_commodity],
                }
            )
            continue

        agg = commodity_qs.aggregate(
            beginning_balance=Sum("beginning_balance"),
            quantity_received=Sum("quantity_received"),
            physical_count=Sum("physical_count"),
        )
        clean_qs = commodity_qs.filter(service_qty_review_flag="")
        dispensed_agg = clean_qs.aggregate(total_dispensed=Sum("quantity_dispensed"))
        flagged_count = commodity_qs.exclude(service_qty_review_flag="").count()

        estimates = [
            estimate_days_out_of_stock(status, period)
            for status, period in commodity_qs.values_list("stock_status", "period")
            if status
        ]
        avg_days_out = round(sum(estimates) / len(estimates), 1) if estimates else None

        rows.append(
            {
                "commodity": moh_commodity,
                "available": True,
                "beginning_balance": agg["beginning_balance"],
                "quantity_received": agg["quantity_received"],
                "total_dispensed": dispensed_agg["total_dispensed"],
                "physical_count": agg["physical_count"],
                "estimated_days_out_of_stock": avg_days_out,
                "flagged_rows_excluded": flagged_count,
                "color": COMMODITY_COLOR_MAP[moh_commodity],
            }
        )

    # LLINs never appears in the loop above (no eCHIS equivalent at all) —
    # add it explicitly so it's visibly "not available", not just absent.
    rows.append(
        {
            "commodity": Commodity.LLINS,
            "available": False,
            "beginning_balance": None,
            "quantity_received": None,
            "total_dispensed": None,
            "physical_count": None,
            "estimated_days_out_of_stock": None,
            "flagged_rows_excluded": 0,
            "color": COMMODITY_COLOR_MAP[Commodity.LLINS],
        }
    )
    return rows


# ---------------------------------------------------------------------------
# Upload form (formerly moh748/forms.py)
# ---------------------------------------------------------------------------


class MOH748UploadForm(forms.Form):
    file = forms.FileField(label="MOH 748 workbook (.xlsx or .xls)")


class CHPCommodityUploadForm(forms.Form):
    file = forms.FileField(label="eCHIS CHP Commodity Stock Flow workbook (.xlsx)")


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------


@login_required
def home(request):
    user = request.user
    facilities = scoped_facilities(user)

    periods = available_periods()
    period = request.GET.get("period") or (periods[0] if periods else "")

    county_id = request.GET.get("county") or None
    sub_county_id = request.GET.get("sub_county") or None
    ward_id = request.GET.get("ward") or None
    facility_id = request.GET.get("facility") or None
    search_query = (request.GET.get("q") or "").strip()
    view_param = request.GET.get("view") or None

    filtered_facilities = apply_geo_filters(
        facilities, county_id=county_id, sub_county_id=sub_county_id, ward_id=ward_id
    )
    if facility_id:
        filtered_facilities = filtered_facilities.filter(id=facility_id)
    if search_query:
        filtered_facilities = filtered_facilities.filter(name__icontains=search_query)

    records = MOH748Record.objects.filter(period=period, facility__in=filtered_facilities).select_related(
        "facility__ward__sub_county__county"
    )

    # Drill-down: land on a county summary, then a sub-county summary. Once
    # a sub-county is chosen, picking a ward or facility directly (via the
    # filter bar) jumps straight to the facility heatmap; but arriving at a
    # sub-county with nothing narrower chosen yet stops at a "choose" screen
    # instead of guessing — she can either see every facility in the
    # sub-county at once, or drill one level further into a ward summary
    # first via ?view=wards / ?view=facilities.
    if ward_id or facility_id:
        level = "facility"
    elif sub_county_id:
        if view_param == "wards":
            level = "ward"
        elif view_param == "facilities":
            level = "facility"
        else:
            level = "choose"
    elif county_id:
        level = "sub_county"
    else:
        level = "county"

    try:
        page_number = int(request.GET.get("page", 1))
    except ValueError:
        page_number = 1

    trend_qs = MOH748Record.objects.filter(facility__in=filtered_facilities)
    trend_data = trend_series(trend_qs)

    # "vs last period" KPI comparisons — the period immediately before the
    # one currently selected, within the same geography/facility scope, so
    # switching counties compares like-for-like rather than mixing scopes.
    try:
        previous_period = periods[periods.index(period) + 1]
    except (ValueError, IndexError):
        previous_period = None
    previous_kpis = None
    if previous_period:
        previous_records = MOH748Record.objects.filter(period=previous_period, facility__in=filtered_facilities)
        previous_kpis = build_kpis(previous_records, filtered_facilities)

    kpis = build_kpis(records, filtered_facilities)

    # Parent-location label shown under each row's name in the summary/
    # facility tables (e.g. a sub-county row shows its county underneath).
    selected_county_name = ""
    if county_id:
        selected_county_name = County.objects.filter(id=county_id).values_list("name", flat=True).first() or ""
    selected_sub_county_name = ""
    if sub_county_id:
        selected_sub_county_name = (
            SubCounty.objects.filter(id=sub_county_id).values_list("name", flat=True).first() or ""
        )

    context = {
        "no_data": not periods,
        "forms_available": FormDefinition.objects.filter(is_active=True),
        "periods": periods,
        "period": period,
        "period_display": period_display(period),
        "filters": filter_options(user, county_id=county_id, sub_county_id=sub_county_id, ward_id=ward_id),
        "selected": {
            "county": county_id or "",
            "sub_county": sub_county_id or "",
            "ward": ward_id or "",
            "facility": facility_id or "",
            "q": search_query,
            "view": view_param or "",
        },
        "level": level,
        "kpis": kpis,
        "kpi_deltas": build_kpi_deltas(kpis, previous_kpis),
        "previous_period_display": period_display(previous_period) if previous_period else "",
        "top_facilities": top_stockout_facilities(records),
        "trend_json": json.dumps(trend_data, cls=DjangoJSONEncoder),
        "trend_has_history": len(trend_data["periods"]) >= 2,
        "trend_months": TREND_MONTHS,
        "can_upload": user.may_upload,
        "commodity_distribution": build_commodity_stockout_distribution(records),
        "selected_county_name": selected_county_name,
        "selected_sub_county_name": selected_sub_county_name,
    }
    context["commodity_distribution_json"] = json.dumps(
        {
            "labels": context["commodity_distribution"]["labels"],
            "counts": context["commodity_distribution"]["counts"],
            "colors": context["commodity_distribution"]["colors"],
        },
        cls=DjangoJSONEncoder,
    )

    if level == "facility":
        context["heatmap"] = build_heatmap(records, page_number=page_number)
    elif level in ("county", "sub_county", "ward"):
        context["geo_summary"] = build_geo_summary(
            records, filtered_facilities, group_by=level, nested=(level == "county")
        )
    # level == "choose": just the two-option screen, no table to build.

    # Stock Balances reflects whatever scope is currently selected — county,
    # one sub-county, one ward, or a single facility — same as the KPI row
    # above, so it stays visible (and correctly narrowed) at every drill-down
    # level rather than disappearing once you pick a specific sub-county.
    context["balance_summary"] = build_balance_summary(records)

    return render(request, "core/home.html", context)


_DRILLDOWN_FIELDS = [
    ("beginning_balance", "Beginning Balance"),
    ("quantity_received", "Quantity Received"),
    ("total_dispensed", "Total Dispensed"),
    ("physical_count", "Physical Count"),
    ("near_expiry_6mo", "Near-Expiry"),
    ("days_out_of_stock", "Days Out of Stock"),
]


@login_required
def facility_drilldown(request, facility_id):
    user = request.user
    facility = get_object_or_404(scoped_facilities(user), pk=facility_id)

    periods = available_periods()
    period = request.GET.get("period") or (periods[0] if periods else "")

    threshold = Threshold.objects.filter(metric_key="days_out_of_stock").order_by("-form_id").first()

    records = MOH748Record.objects.filter(facility=facility, period=period).order_by("commodity")

    rows = []
    stockout_count = 0
    near_expiry_total = None
    any_days_reported = False
    for record in records:
        status = None
        if record.days_out_of_stock is not None:
            any_days_reported = True
            if threshold:
                status = threshold.status_for(record.days_out_of_stock)
            if record.days_out_of_stock > 0:
                stockout_count += 1
        if record.near_expiry_6mo is not None:
            near_expiry_total = (near_expiry_total or 0) + record.near_expiry_6mo
        rows.append({"record": record, "status": status, "color": COMMODITY_COLOR_MAP.get(record.commodity)})

    # A blank cell here can mean "nothing to report" or "left blank on
    # submission" — and this page is read commodity-by-commodity, where that
    # distinction matters most. Flag any field missing across every
    # commodity this period so it's called out once at the top, instead of
    # leaving five separate dashes for Lynne to notice and guess about.
    missing_fields = []
    if rows:
        for field_key, field_label in _DRILLDOWN_FIELDS:
            if all(getattr(row["record"], field_key) is None for row in rows):
                missing_fields.append(field_label)

    # Same "Days-Out-of-Stock Trend" chart as the main dashboard, just
    # scoped to this one facility instead of a county/ward/sub-county — she
    # asked why picking a specific facility lost the trend view entirely;
    # it never had one, this gives it the same chart everywhere else does.
    trend_data = trend_series(MOH748Record.objects.filter(facility=facility))

    context = {
        "facility": facility,
        "periods": periods,
        "period": period,
        "period_display": period_display(period),
        "threshold": threshold,
        "rows": rows,
        "stockout_count": stockout_count,
        "commodities_tracked": len(rows),
        "commodities_total": len(Commodity),
        "any_days_reported": any_days_reported,
        "near_expiry_total": near_expiry_total,
        "missing_fields": missing_fields,
        "trend_json": json.dumps(trend_data, cls=DjangoJSONEncoder),
        "trend_has_history": len(trend_data["periods"]) >= 2,
        "trend_months": TREND_MONTHS,
    }
    return render(request, "core/facility_drilldown.html", context)


@login_required
def chp_commodity_home(request):
    user = request.user
    areas = chp_scoped_areas(user)

    periods = chp_available_periods()
    period = request.GET.get("period") or (periods[0] if periods else "")

    county_id = request.GET.get("county") or None
    sub_county_id = request.GET.get("sub_county") or None
    chu_id = request.GET.get("chu") or None
    area_id = request.GET.get("area") or None
    search_query = (request.GET.get("q") or "").strip()
    view_param = request.GET.get("view") or None

    filtered_areas = apply_chp_geo_filters(areas, county_id=county_id, sub_county_id=sub_county_id, chu_id=chu_id)
    if area_id:
        filtered_areas = filtered_areas.filter(id=area_id)
    if search_query:
        filtered_areas = filtered_areas.filter(name__icontains=search_query)

    records = CHPCommodityRecord.objects.filter(period=period, chp_area__in=filtered_areas).select_related(
        "chp_area__community_health_unit__sub_county__county"
    )

    # Same drill-down shape as the MOH 748 dashboard: land on a county
    # summary, then sub-county, then a "choose CHUs or CHP areas" screen
    # rather than guessing which one she wants.
    if chu_id or area_id:
        level = "area"
    elif sub_county_id:
        if view_param == "chus":
            level = "chu"
        elif view_param == "areas":
            level = "area"
        else:
            level = "choose"
    elif county_id:
        level = "sub_county"
    else:
        level = "county"

    try:
        page_number = int(request.GET.get("page", 1))
    except ValueError:
        page_number = 1

    kpis = build_chp_kpis(records, filtered_areas)

    selected_county_name = ""
    if county_id:
        selected_county_name = County.objects.filter(id=county_id).values_list("name", flat=True).first() or ""
    selected_sub_county_name = ""
    if sub_county_id:
        selected_sub_county_name = (
            SubCounty.objects.filter(id=sub_county_id).values_list("name", flat=True).first() or ""
        )

    context = {
        "no_data": not periods,
        "periods": periods,
        "period": period,
        "period_display": period_display(period),
        "filters": chp_filter_options(user, county_id=county_id, sub_county_id=sub_county_id, chu_id=chu_id),
        "selected": {
            "county": county_id or "",
            "sub_county": sub_county_id or "",
            "chu": chu_id or "",
            "area": area_id or "",
            "q": search_query,
            "view": view_param or "",
        },
        "level": level,
        "kpis": kpis,
        "can_upload": user.may_upload,
        "selected_county_name": selected_county_name,
        "selected_sub_county_name": selected_sub_county_name,
    }

    if level == "area":
        context["heatmap"] = build_chp_heatmap(records, page_number=page_number)
    elif level in ("county", "sub_county", "chu"):
        context["geo_summary"] = build_chp_geo_summary(records, filtered_areas, group_by=level)
    # level == "choose": just the two-option screen, no table to build.

    # Commodity Balances reflects whatever scope is currently selected —
    # county, one sub-county, one CHU, or a single CHP area — same as the
    # KPI row above, so it stays visible (and correctly narrowed) at every
    # drill-down level. This is the "actual balances, not just stockout
    # status" view Lynne asked for alongside the heatmap.
    context["balance_summary"] = build_chp_balance_summary(records)

    # Preview only — never written to MOH748Record. See
    # build_chp_moh748_preview()'s docstring.
    context["moh748_preview"] = build_chp_moh748_preview(records)

    return render(request, "core/chp_home.html", context)


def _can_upload(user):
    return user.is_authenticated and user.may_upload


def _upload_page_context(*, moh748_form=None, moh748_result=None, chp_form=None, chp_result=None):
    """
    Shared context for the one upload page, which holds both the MOH 748
    upload card and the CHP Commodity Stock Flow upload card side by side —
    one page you go to for uploads, not one per form type. Each card posts
    to its own view (below), but both always render together so submitting
    one never loses sight of the other's history.

    live_record_count is how many of an upload's records are still on file
    — re-uploading the same period replaces matching records in place (see
    parsing.py), so an older upload for a period that's since been
    re-uploaded is left with 0 live records even though it once created
    some. That's the "Superseded" case the template flags for both tables.
    """
    moh748_uploads = (
        MOH748Upload.objects.select_related("uploaded_by")
        .annotate(live_record_count=Count("records"))
        .order_by("-uploaded_at")
    )
    chp_uploads = (
        CHPCommodityUpload.objects.select_related("uploaded_by")
        .annotate(live_record_count=Count("records"))
        .order_by("-uploaded_at")
    )
    return {
        "form": moh748_form or MOH748UploadForm(),
        "result": moh748_result,
        "uploads": moh748_uploads,
        "chp_form": chp_form or CHPCommodityUploadForm(),
        "chp_result": chp_result,
        "chp_uploads": chp_uploads,
    }


@login_required
@user_passes_test(_can_upload, login_url="home")
def upload_moh748(request):
    result = None
    form = MOH748UploadForm()
    if request.method == "POST":
        form = MOH748UploadForm(request.POST, request.FILES)
        if form.is_valid():
            uploaded = request.FILES["file"]
            result = parse_moh748_workbook(
                uploaded,
                source_filename=uploaded.name,
                uploaded_by=request.user,
            )
            messages.success(
                request,
                f"Uploaded {uploaded.name} — {result.facility_rows_parsed} facilities, "
                f"{result.record_rows_created} records.",
            )
            return redirect("upload")

    return render(request, "core/upload.html", _upload_page_context(moh748_form=form, moh748_result=result))


@login_required
@user_passes_test(_can_upload, login_url="home")
def delete_moh748_upload(request, upload_id):
    upload = get_object_or_404(MOH748Upload, pk=upload_id)
    if request.method == "POST":
        label = f"{upload.period or 'unlabeled period'} — {upload.source_filename}"
        upload.delete()
        messages.success(request, f"Deleted upload: {label}.")
    return redirect("upload")


@login_required
@user_passes_test(_can_upload, login_url="home")
def upload_chp_commodity(request):
    result = None
    form = CHPCommodityUploadForm()
    if request.method == "POST":
        form = CHPCommodityUploadForm(request.POST, request.FILES)
        if form.is_valid():
            uploaded = request.FILES["file"]
            try:
                result = parse_chp_commodity_workbook(
                    uploaded,
                    source_filename=uploaded.name,
                    uploaded_by=request.user,
                )
            except ValueError as exc:
                form.add_error("file", str(exc))
            else:
                notes = []
                if result.unresolved_geography_rows:
                    notes.append(f"{result.unresolved_geography_rows} rows kept under 'Unresolved geography'")
                if result.unmapped_chw_rows:
                    notes.append(f"{result.unmapped_chw_rows} rows with no currently mapped CHW")
                extra = f" ({'; '.join(notes)})" if notes else ""
                messages.success(
                    request,
                    f"Uploaded {uploaded.name} — {result.area_rows_parsed} area rows, "
                    f"{result.record_rows_created} records{extra}.",
                )
                return redirect("upload")

    return render(request, "core/upload.html", _upload_page_context(chp_form=form, chp_result=result))


@login_required
@user_passes_test(_can_upload, login_url="home")
def delete_chp_commodity_upload(request, upload_id):
    upload = get_object_or_404(CHPCommodityUpload, pk=upload_id)
    if request.method == "POST":
        label = f"{upload.period or 'unlabeled period'} — {upload.source_filename}"
        upload.delete()
        messages.success(request, f"Deleted upload: {label}.")
    return redirect("upload")
