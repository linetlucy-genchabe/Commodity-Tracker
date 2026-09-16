"""
All views for the Commodity Tracker, plus the query-helper functions that
build dashboard context and the tiny upload form. Kept in one file — see
README "Project layout".
"""

import calendar
import csv
import json
from datetime import date

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required, user_passes_test
from django.core.paginator import Paginator
from django.core.serializers.json import DjangoJSONEncoder
from django.db.models import Avg, Count, Max, Sum
from django.http import HttpResponse
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
# Each S11 voucher is a full card (13 commodity rows plus a header), so a
# much smaller page size than the 25-row heatmap keeps the page scrollable
# rather than one huge stack of cards.
S11_PAGE_SIZE = 5


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

# "Unit of Issue" as it appears on eCHIS's own "Balance on Hand" screen —
# Form S11 (Requisition and Issue Voucher) has this exact column, so the
# generated S11 uses the same wording eCHIS already shows for each
# commodity. PARACETAMOL here covers the tablet form only (500mg) — eCHIS's
# Commodities Order form also has a separate Paracetamol 120mg/5ml
# Suspension line (unit: Bottles) that isn't a CHPCommodity of its own, so
# it never appears on this page. Anything not listed falls back to "Units".
CHP_S11_UNIT_OF_ISSUE = {
    CHPCommodity.AL_6: "Packs",
    CHPCommodity.AL_12: "Packs",
    CHPCommodity.AL_18: "Packs",
    CHPCommodity.AL_24: "Packs",
    CHPCommodity.RDTS: "Kits",
    CHPCommodity.AMOXICILLIN_DT250: "Tablets",
    CHPCommodity.ORS_ZINC: "Pieces",
    CHPCommodity.ORS_SACHETS: "Sachets",
    CHPCommodity.ZINC_SULPHATE: "Tablets",
    CHPCommodity.PARACETAMOL: "Tablets",
    CHPCommodity.GLUCOMETER_STRIPS: "Strips",
    CHPCommodity.GLOVES: "Pairs",
    CHPCommodity.DISPENSING_ENVELOPES: "Pieces",
}


def chp_available_periods():
    return list(
        CHPCommodityRecord.objects.exclude(period="")
        .order_by("-period")
        .values_list("period", flat=True)
        .distinct()
    )


def _shift_month(d, months):
    """d is always day=1; shift by `months` (negative goes back)."""
    m = d.month - 1 + months
    y = d.year + m // 12
    m = m % 12 + 1
    return date(y, m, 1)


def _ym(d):
    return f"{d.year:04d}-{d.month:02d}"


def chp_period_presets():
    """
    Calendar-based period-filter shortcuts Lynne asked for: Current Month /
    Last Month / Last 2 Months / Last Full Quarter -- computed from today's
    real date, not from what data happens to exist, so a shortcut for a
    period nothing has been uploaded for yet still shows up (and the page
    renders blank), exactly as she asked ("whether with data or not").

    "Last 2 Months" is the two most recently COMPLETED months, excluding
    the current one in progress -- e.g. in September, that's July+August,
    not August+September. "Last Full Quarter" is the calendar quarter
    before the one the current month falls in, since that one isn't over
    yet -- e.g. in September (Q3: Jul-Sep), that's Q2 (Apr-Jun).

    Each entry's period/start/end are plain "YYYY-MM" strings, safe to
    compare lexicographically (period__gte/__lte) since they're always
    zero-padded.
    """
    this_month = date(date.today().year, date.today().month, 1)
    current = this_month
    last_month = _shift_month(this_month, -1)
    two_months_ago = _shift_month(this_month, -2)

    current_quarter_start_month = ((this_month.month - 1) // 3) * 3 + 1
    current_quarter_start = date(this_month.year, current_quarter_start_month, 1)
    last_full_quarter_end = _shift_month(current_quarter_start, -1)
    last_full_quarter_start = _shift_month(last_full_quarter_end, -2)

    return [
        {
            "key": "current_month",
            "label": "Current Month",
            "kind": "single",
            "period": _ym(current),
            "display": period_display(_ym(current)),
        },
        {
            "key": "last_month",
            "label": "Last Month",
            "kind": "single",
            "period": _ym(last_month),
            "display": period_display(_ym(last_month)),
        },
        {
            "key": "last_2_months",
            "label": "Last 2 Months",
            "kind": "range",
            "start": _ym(two_months_ago),
            "end": _ym(last_month),
            "display": f"{period_display(_ym(two_months_ago))} – {period_display(_ym(last_month))}",
        },
        {
            "key": "last_full_quarter",
            "label": "Last Full Quarter",
            "kind": "range",
            "start": _ym(last_full_quarter_start),
            "end": _ym(last_full_quarter_end),
            "display": f"{period_display(_ym(last_full_quarter_start))} – {period_display(_ym(last_full_quarter_end))}",
        },
    ]


def chp_resolve_period_range(range_key):
    """(start, end) "YYYY-MM" tuple for a range preset key, or None if unknown."""
    for preset in chp_period_presets():
        if preset["key"] == range_key and preset["kind"] == "range":
            return preset["start"], preset["end"]
    return None


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
    a weeks-of-stock number gets a colored pill on the CHP dashboard: under
    2 weeks is stockout/low stock (RED), 2-4 weeks is moderate (AMBER), 4-6
    weeks is adequate (GREEN), and over 6 weeks is overstocked -- also red,
    per Lynne's request, but its own lighter shade (RED_LIGHT) so a
    dangerously-low CHP and an overstocked one never look identical at a
    glance. Replaces the earlier, coarser 3-band version (<1 red / <=2
    amber / else green), which itself replaced a 2-band original.
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
    return "RED_LIGHT"


def chp_weeks_bands(records_qs):
    """
    One weeks-of-stock band per CHP -- its own average across whatever
    commodities it has in scope (same average the Avg. Weeks of Stock
    figure already used), so a CHP lands in exactly one band rather than
    being counted once per commodity. Powers the four top-of-page status
    cards Lynne asked for: Adequate (4-6 wks) / Moderate (2-4 wks) /
    Stockout or Low Stock (0-2 wks) / Overstocked (6+ wks) -- same
    boundaries and colors as weeks_of_stock_severity() everywhere else on
    this dashboard, just counted per CHP instead of rendered as one pill.
    A CHP with no weeks-of-stock data anywhere in scope isn't in any band.
    """
    per_area = (
        records_qs.exclude(weeks_of_stock__isnull=True)
        .values("chp_area_id")
        .annotate(avg_weeks=Avg("weeks_of_stock"))
    )
    bands = {"GREEN": 0, "AMBER": 0, "RED": 0, "RED_LIGHT": 0}
    for row in per_area:
        band = weeks_of_stock_severity(row["avg_weeks"])
        if band:
            bands[band] += 1
    return {
        "adequate": bands["GREEN"],
        "moderate": bands["AMBER"],
        "stockout_low": bands["RED"],
        "overstocked": bands["RED_LIGHT"],
        "reporting_with_weeks": len(per_area),
    }


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
        "weeks_bands": chp_weeks_bands(records_qs),
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


def _build_chp_heatmap_rows(records_qs):
    """
    The full, unpaginated row list behind build_chp_heatmap() — same rows,
    same worst-first sort, just without slicing to one page. Split out so
    the CHP Stock Status CSV export can write every CHP in scope, not just
    whatever page happens to be on screen.
    """
    area_ids = list(records_qs.values_list("chp_area_id", flat=True).distinct())
    areas = list(
        CHPArea.objects.filter(id__in=area_ids).select_related("community_health_unit__sub_county__county", "county")
    )

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
    return rows


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
    rows = _build_chp_heatmap_rows(records_qs)
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


def build_chp_balance_summary(records_qs, *, period_range=None):
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

    period_range, when given, is an (earliest, latest) "YYYY-MM" tuple —
    the multi-month rollups behind Last 2 Months / Last Full Quarter.
    records_qs already spans every period in the range; Received,
    Dispensed and Avg Weeks of Stock genuinely happened/applied across the
    whole range, so they still sum/average over all of it, but Beginning
    Balance comes from the range's first month only and Ending Balance /
    Physical Count / Stock on Hand (the "current state" figures) come from
    its last month only — summing those across months would double-count
    stock that was never actually received twice.
    """
    rows = []
    for commodity in CHPCommodity:
        commodity_qs = records_qs.filter(commodity=commodity)

        flow_agg = commodity_qs.aggregate(
            quantity_received=Sum("quantity_received"),
            avg_weeks_of_stock=Avg("weeks_of_stock"),
        )

        if period_range:
            earliest, latest = period_range
            beginning_balance = commodity_qs.filter(period=earliest).aggregate(v=Sum("beginning_balance"))["v"]
            state_agg = commodity_qs.filter(period=latest).aggregate(
                ending_balance=Sum("ending_balance"),
                physical_count=Sum("physical_count"),
                stock_on_hand=Sum("stock_on_hand"),
            )
        else:
            beginning_balance = commodity_qs.aggregate(v=Sum("beginning_balance"))["v"]
            state_agg = commodity_qs.aggregate(
                ending_balance=Sum("ending_balance"),
                physical_count=Sum("physical_count"),
                stock_on_hand=Sum("stock_on_hand"),
            )

        clean_qs = commodity_qs.filter(service_qty_review_flag="")
        dispensed_agg = clean_qs.aggregate(quantity_dispensed=Sum("quantity_dispensed"))
        flagged_count = commodity_qs.exclude(service_qty_review_flag="").count()

        avg_weeks = flow_agg["avg_weeks_of_stock"]
        avg_weeks_rounded = round(float(avg_weeks), 1) if avg_weeks is not None else None
        rows.append(
            {
                "commodity": commodity,
                "beginning_balance": beginning_balance,
                "quantity_received": flow_agg["quantity_received"],
                "quantity_dispensed": dispensed_agg["quantity_dispensed"],
                "ending_balance": state_agg["ending_balance"],
                "physical_count": state_agg["physical_count"],
                "stock_on_hand": state_agg["stock_on_hand"],
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
# The physical MOH 748 form's main table tracks AL 6s/12s/18s/24s plus
# Malaria RDTs — five rows, columns A-F (Beginning Balance, Quantity
# Received, Total Dispensed, Losses Excl. Expiries, Balance/Physical
# Count, Days Out of Stock). RDTs has no Commodity (748) enum member of
# its own — 748's Commodity enum only ever needed LLINs + the four AL
# sizes — so it's listed here directly as (CHPCommodity, label, color)
# rather than through a CHP-commodity -> Commodity mapping. LLINs is on
# the physical form too but has no CHP Commodity Stock Flow equivalent at
# all, so it's appended after the loop, always "not available from eCHIS"
# (same pattern used for the Losses column below — see that column's note).
# ---------------------------------------------------------------------------

CHP_TO_MOH748_COMMODITY = {
    CHPCommodity.AL_6: Commodity.AL_6,
    CHPCommodity.AL_12: Commodity.AL_12,
    CHPCommodity.AL_18: Commodity.AL_18,
    CHPCommodity.AL_24: Commodity.AL_24,
}

MOH748_PREVIEW_ROWS = [
    (CHPCommodity.AL_6, Commodity.AL_6.label, COMMODITY_COLOR_MAP[Commodity.AL_6]),
    (CHPCommodity.AL_12, Commodity.AL_12.label, COMMODITY_COLOR_MAP[Commodity.AL_12]),
    (CHPCommodity.AL_18, Commodity.AL_18.label, COMMODITY_COLOR_MAP[Commodity.AL_18]),
    (CHPCommodity.AL_24, Commodity.AL_24.label, COMMODITY_COLOR_MAP[Commodity.AL_24]),
    (CHPCommodity.RDTS, "Malaria RDTs", CHP_COMMODITY_COLOR_MAP[CHPCommodity.RDTS]),
]


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


def build_chp_moh748_preview(records_qs, *, period_range=None):
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

    losses_excl_expiries is always None — column D on the physical form,
    genuinely not present anywhere in eCHIS's export at this reporting
    grain (confirmed against the Data Dictionary sheet of Lynne's own
    source workbook: "Requested columns included but blank... eCHIS has
    separate stock amendment/discrepancy events, not direct positive/
    negative fields"). Kept as an explicit blank column, same "shown but
    marked unavailable" treatment as the LLINs row, rather than dropped.

    period_range, when given, is an (earliest, latest) "YYYY-MM" tuple —
    same Last 2 Months / Last Full Quarter rollup as
    build_chp_balance_summary(): Beginning Balance from the range's first
    month, Physical Count from its last month, Received/Dispensed/the
    days-out-of-stock estimate summed or averaged across the whole range.
    """
    rows = []
    for chp_commodity, label, color in MOH748_PREVIEW_ROWS:
        commodity_qs = records_qs.filter(commodity=chp_commodity)
        if not commodity_qs.exists():
            rows.append(
                {
                    "commodity_label": label,
                    "available": False,
                    "beginning_balance": None,
                    "quantity_received": None,
                    "total_dispensed": None,
                    "losses_excl_expiries": None,
                    "physical_count": None,
                    "estimated_days_out_of_stock": None,
                    "flagged_rows_excluded": 0,
                    "color": color,
                }
            )
            continue

        flow_agg = commodity_qs.aggregate(quantity_received=Sum("quantity_received"))

        if period_range:
            earliest, latest = period_range
            beginning_balance = commodity_qs.filter(period=earliest).aggregate(v=Sum("beginning_balance"))["v"]
            physical_count = commodity_qs.filter(period=latest).aggregate(v=Sum("physical_count"))["v"]
        else:
            beginning_balance = commodity_qs.aggregate(v=Sum("beginning_balance"))["v"]
            physical_count = commodity_qs.aggregate(v=Sum("physical_count"))["v"]

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
                "commodity_label": label,
                "available": True,
                "beginning_balance": beginning_balance,
                "quantity_received": flow_agg["quantity_received"],
                "total_dispensed": dispensed_agg["total_dispensed"],
                "losses_excl_expiries": None,
                "physical_count": physical_count,
                "estimated_days_out_of_stock": avg_days_out,
                "flagged_rows_excluded": flagged_count,
                "color": color,
            }
        )

    # LLINs never appears in the loop above (no eCHIS equivalent at all) —
    # add it explicitly so it's visibly "not available", not just absent.
    rows.append(
        {
            "commodity_label": Commodity.LLINS.label,
            "available": False,
            "beginning_balance": None,
            "quantity_received": None,
            "total_dispensed": None,
            "losses_excl_expiries": None,
            "physical_count": None,
            "estimated_days_out_of_stock": None,
            "flagged_rows_excluded": 0,
            "color": COMMODITY_COLOR_MAP[Commodity.LLINS],
        }
    )
    return rows


# ---------------------------------------------------------------------------
# Generated S11 (Requisition and Issue Voucher) — draft, per CHU
#
# Lynne shared a photo of the actual paper Form S11: it wants, per
# commodity, a Quantity Required (what the CHU/CHA asked for) and a
# Quantity Issued (what the issuing store actually gave out), plus a
# Code No./Value/Remarks that this dashboard has no source for at all.
#
# Checked against the raw commodities_order eCHIS export (the "second data"
# file): its required_* fields — the digital equivalent of "Quantity
# Required" — are blank or zero in effectively every row (only 8 of 54
# commodity columns had ANY non-zero value across 738 submissions, and only
# 9 of 162 CHUs ever recorded one). So Quantity Required isn't something
# this dashboard can populate from eCHIS at all right now — it's a paper-
# only figure — and is always shown as 0, same "zero-fill for a draft"
# treatment as the rest of the Generated MOH 748 page. Quantity Issued
# reuses quantity_received from CHP Commodity Stock Flow: what a CHU
# received from its issuing point over the period IS what that point
# issued, so it's the same number from the other side of the transaction —
# and unlike Required, this one is real eCHIS data, not zero-filled.
# ---------------------------------------------------------------------------


def _build_chp_s11_vouchers(records_qs, areas_qs):
    """
    One voucher per CHU in scope — Form S11 is filled "to (point of use):
    COMMUNITY", i.e. at CHU level, not per CHP area, so every CHP area's
    records within a CHU are pooled into that CHU's one voucher. An area
    with no resolved CHU still gets counted, as one combined "Unresolved
    CHU" voucher, rather than silently dropped — same principle as
    build_chp_geo_summary()'s unresolved row.
    """

    def _rows_for(unit_records):
        rows = []
        for commodity in CHPCommodity:
            commodity_qs = unit_records.filter(commodity=commodity)
            quantity_issued = commodity_qs.aggregate(v=Sum("quantity_received"))["v"]
            rows.append(
                {
                    "commodity": commodity,
                    "unit_of_issue": CHP_S11_UNIT_OF_ISSUE.get(commodity, "Units"),
                    "quantity_issued": quantity_issued,
                    "color": CHP_COMMODITY_COLOR_MAP[commodity],
                }
            )
        return rows

    unit_ids = (
        areas_qs.exclude(community_health_unit__isnull=True)
        .values_list("community_health_unit_id", flat=True)
        .distinct()
    )
    chus = (
        CommunityHealthUnit.objects.filter(id__in=unit_ids)
        .select_related("sub_county__county", "facility")
        .order_by("sub_county__county__name", "sub_county__name", "name")
    )

    vouchers = []
    for chu in chus:
        unit_areas = areas_qs.filter(community_health_unit=chu)
        unit_records = records_qs.filter(chp_area__in=unit_areas)
        vouchers.append(
            {
                "chu": chu,
                "unresolved": False,
                "areas_count": unit_areas.count(),
                "rows": _rows_for(unit_records),
            }
        )

    unresolved_areas = areas_qs.filter(community_health_unit__isnull=True)
    if unresolved_areas.exists():
        unresolved_records = records_qs.filter(chp_area__in=unresolved_areas)
        vouchers.append(
            {
                "chu": None,
                "unresolved": True,
                "areas_count": unresolved_areas.count(),
                "rows": _rows_for(unresolved_records),
            }
        )

    return vouchers


def build_chp_s11_summary(records_qs, areas_qs, *, page_number=1):
    """Paginated wrapper around _build_chp_s11_vouchers() for the on-screen page — see that function's docstring."""
    vouchers = _build_chp_s11_vouchers(records_qs, areas_qs)
    paginator = Paginator(vouchers, S11_PAGE_SIZE)
    page_obj = paginator.get_page(page_number)
    return {"vouchers": page_obj.object_list, "page": page_obj}


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
def _chp_resolve_scope(request):
    """
    GET-param resolution shared by the CHP dashboard, the Generated MOH 748
    page, and their CSV downloads, so a download always reflects exactly
    the period + geography filters on screen — never the whole table
    regardless of what's selected.

    One dropdown, one param: Lynne asked for the Period filter and the
    quick-range presets to live in a single dropdown rather than a
    dropdown plus a separate row of preset chips. So the sidebar's Period
    <select> carries both — a literal "YYYY-MM" option for each real
    period, and one "range:<key>" option per chp_period_presets() entry
    (e.g. "range:last_full_quarter" or "range:current_month") — all under
    the one ?period= param. A "range:" prefix is peeled off and looked up
    against chp_period_presets(): a "range"-kind preset (Last 2 Months,
    Last Full Quarter) resolves to a multi-month window; a "single"-kind
    preset (Current Month, Last Month) just resolves to that preset's own
    literal period -- it's in the same dropdown for convenience, but
    behaves exactly like picking that YYYY-MM directly, no multi-month
    aggregation involved. Anything without a "range:" prefix is a literal
    period, exactly as before. ?range=<key> is still accepted on its own
    (range-kind keys only) as a fallback for any old bookmarked/shared link.

    "records" is every record in the resolved window (period__gte/__lte,
    safe for these zero-padded "YYYY-MM" strings) or just the one period;
    "period_range" is the (earliest, latest) tuple the range-aware builder
    functions need for their Beginning/Ending Balance split; "latest_records"
    is just the window's last month -- what the heatmap uses, since a
    stock-status snapshot can't sensibly be rolled up across months the
    way a total can. An unrecognised/missing range falls back to
    single-period mode.
    """
    user = request.user
    areas = chp_scoped_areas(user)

    periods = chp_available_periods()
    period_param = request.GET.get("period") or ""

    selected_preset = None
    if period_param.startswith("range:"):
        preset_key = period_param[len("range:"):]
        selected_preset = next((p for p in chp_period_presets() if p["key"] == preset_key), None)

    range_key = None
    resolved_range = None
    selected_single_preset = None
    if selected_preset and selected_preset["kind"] == "range":
        range_key = selected_preset["key"]
        resolved_range = (selected_preset["start"], selected_preset["end"])
    elif selected_preset and selected_preset["kind"] == "single":
        selected_single_preset = selected_preset["key"]
    elif not period_param.startswith("range:"):
        # Backward-compat: an old link/bookmark using the standalone
        # ?range= param (range-kind keys only).
        legacy_range_key = request.GET.get("range") or None
        if legacy_range_key:
            resolved_range = chp_resolve_period_range(legacy_range_key)
            if resolved_range:
                range_key = legacy_range_key

    if resolved_range:
        period = periods[0] if periods else ""
    elif selected_single_preset:
        period = selected_preset["period"]
    else:
        period = (period_param if not period_param.startswith("range:") else "") or (
            periods[0] if periods else ""
        )

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

    base_qs = CHPCommodityRecord.objects.filter(chp_area__in=filtered_areas).select_related(
        "chp_area__community_health_unit__sub_county__county"
    )

    if resolved_range:
        earliest, latest = resolved_range
        records = base_qs.filter(period__gte=earliest, period__lte=latest)
        latest_records = base_qs.filter(period=latest)
        period_display_label = next(
            (p["display"] for p in chp_period_presets() if p["key"] == range_key), f"{earliest} – {latest}"
        )
    else:
        range_key = None
        records = base_qs.filter(period=period)
        latest_records = records
        period_display_label = period_display(period)

    return {
        "user": user,
        "periods": periods,
        "period": period,
        "range_key": range_key,
        "selected_single_preset": selected_single_preset,
        "period_range": resolved_range,
        "period_display_label": period_display_label,
        "county_id": county_id,
        "sub_county_id": sub_county_id,
        "chu_id": chu_id,
        "area_id": area_id,
        "search_query": search_query,
        "view_param": view_param,
        "filtered_areas": filtered_areas,
        "records": records,
        "latest_records": latest_records,
    }


def chp_commodity_home(request):
    scope = _chp_resolve_scope(request)
    user = scope["user"]
    periods = scope["periods"]
    period = scope["period"]
    range_key = scope["range_key"]
    period_range = scope["period_range"]
    county_id = scope["county_id"]
    sub_county_id = scope["sub_county_id"]
    chu_id = scope["chu_id"]
    area_id = scope["area_id"]
    search_query = scope["search_query"]
    view_param = scope["view_param"]
    filtered_areas = scope["filtered_areas"]
    records = scope["records"]
    latest_records = scope["latest_records"]

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
    # Shown as a page heading once someone has drilled down to one specific
    # CHP, so that screen reads as "you're looking at Grace Akinyi Olonde
    # Area" rather than a generic "CHP Area" table with one row in it.
    selected_area_name = ""
    if area_id:
        selected_area_name = CHPArea.objects.filter(id=area_id).values_list("name", flat=True).first() or ""

    context = {
        "no_data": not periods,
        "periods": periods,
        "period": period,
        "period_display": scope["period_display_label"],
        "period_presets": chp_period_presets(),
        "selected_range": range_key or "",
        "selected_single_preset": scope["selected_single_preset"] or "",
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
        "selected_area_name": selected_area_name,
    }

    # The heatmap is a point-in-time snapshot (this area's status THIS
    # month), which doesn't roll up across months the way a total does —
    # so in range mode it always shows the range's last month, same as
    # picking that single month on its own would.
    #
    # Skipped once a single CHP is selected (area_id set): at that point
    # it's always exactly one row, and everything in it — which
    # commodities are low, the status colors — is already visible in
    # Commodity Balances and the Generated MOH 748 preview above it.
    # Lynne flagged this as duplicated work; the heatmap earns its place
    # comparing MANY CHPs at once (a CHU or sub-county in view), not
    # re-describing the one CHP already on screen.
    if level == "area" and not area_id:
        context["heatmap"] = build_chp_heatmap(latest_records, page_number=page_number)
    elif level in ("county", "sub_county", "chu"):
        context["geo_summary"] = build_chp_geo_summary(records, filtered_areas, group_by=level)
    # level == "choose": just the two-option screen, no table to build.

    # Commodity Balances reflects whatever scope is currently selected —
    # county, one sub-county, one CHU, or a single CHP area — same as the
    # KPI row above, so it stays visible (and correctly narrowed) at every
    # drill-down level. This is the "actual balances, not just stockout
    # status" view Lynne asked for alongside the heatmap.
    context["balance_summary"] = build_chp_balance_summary(records, period_range=period_range)

    # Preview only — never written to MOH748Record. See
    # build_chp_moh748_preview()'s docstring.
    context["moh748_preview"] = build_chp_moh748_preview(records, period_range=period_range)

    return render(request, "core/chp_home.html", context)


@login_required
def chp_moh748_page(request):
    """
    The standalone "MOH 748" nav tab — the Generated MOH 748 Preview,
    entirely eCHIS-derived, on its own page rather than embedded under the
    CHP Stock Flow dashboard. No facility upload is involved and none is
    required: Lynne was explicit that nobody is uploading a past month's
    file for this — the 748 shown here is always generated fresh from
    whatever eCHIS backend data (CHP Commodity Stock Flow) is on file,
    narrowed by whichever county/sub-county/CHU/CHP filters are selected,
    exactly like the same table already embedded in the CHP dashboard.
    Shares its scope resolution, period presets and geography filters with
    that dashboard (_chp_resolve_scope, chp_filter_options,
    chp_period_presets) so the two pages never disagree about what's in
    view for the same filter selection.
    """
    scope = _chp_resolve_scope(request)
    user = scope["user"]
    county_id = scope["county_id"]
    sub_county_id = scope["sub_county_id"]
    chu_id = scope["chu_id"]
    area_id = scope["area_id"]

    selected_county_name = ""
    if county_id:
        selected_county_name = County.objects.filter(id=county_id).values_list("name", flat=True).first() or ""
    selected_sub_county_name = ""
    if sub_county_id:
        selected_sub_county_name = (
            SubCounty.objects.filter(id=sub_county_id).values_list("name", flat=True).first() or ""
        )
    selected_area_name = ""
    if area_id:
        selected_area_name = CHPArea.objects.filter(id=area_id).values_list("name", flat=True).first() or ""

    context = {
        "no_data": not scope["periods"],
        "periods": scope["periods"],
        "period": scope["period"],
        "period_display": scope["period_display_label"],
        "period_presets": chp_period_presets(),
        "selected_range": scope["range_key"] or "",
        "selected_single_preset": scope["selected_single_preset"] or "",
        "filters": chp_filter_options(user, county_id=county_id, sub_county_id=sub_county_id, chu_id=chu_id),
        "selected": {
            "county": county_id or "",
            "sub_county": sub_county_id or "",
            "chu": chu_id or "",
            "area": area_id or "",
            "q": scope["search_query"],
            "view": scope["view_param"] or "",
        },
        "selected_county_name": selected_county_name,
        "selected_sub_county_name": selected_sub_county_name,
        "selected_area_name": selected_area_name,
        "moh748_preview": build_chp_moh748_preview(scope["records"], period_range=scope["period_range"]),
    }
    return render(request, "core/chp_moh748.html", context)


@login_required
def chp_s11_page(request):
    """
    The standalone "S11" nav tab — a draft Form S11 (Requisition and Issue
    Voucher), one card per CHU in the current scope, generated from
    whatever eCHIS backend data (CHP Commodity Stock Flow) is on file.
    Shares scope resolution, period presets and geography filters with the
    MOH 748 and CHP Stock Flow pages (_chp_resolve_scope, chp_filter_options,
    chp_period_presets), so all three never disagree about what's in view
    for the same filter selection. See _build_chp_s11_vouchers()'s docstring
    for what is and isn't populated from real data on this page.
    """
    scope = _chp_resolve_scope(request)
    user = scope["user"]
    county_id = scope["county_id"]
    sub_county_id = scope["sub_county_id"]
    chu_id = scope["chu_id"]
    area_id = scope["area_id"]

    selected_county_name = ""
    if county_id:
        selected_county_name = County.objects.filter(id=county_id).values_list("name", flat=True).first() or ""
    selected_sub_county_name = ""
    if sub_county_id:
        selected_sub_county_name = (
            SubCounty.objects.filter(id=sub_county_id).values_list("name", flat=True).first() or ""
        )
    selected_area_name = ""
    if area_id:
        selected_area_name = CHPArea.objects.filter(id=area_id).values_list("name", flat=True).first() or ""

    try:
        page_number = int(request.GET.get("page", 1))
    except ValueError:
        page_number = 1

    context = {
        "no_data": not scope["periods"],
        "periods": scope["periods"],
        "period": scope["period"],
        "period_display": scope["period_display_label"],
        "period_presets": chp_period_presets(),
        "selected_range": scope["range_key"] or "",
        "selected_single_preset": scope["selected_single_preset"] or "",
        "filters": chp_filter_options(user, county_id=county_id, sub_county_id=sub_county_id, chu_id=chu_id),
        "selected": {
            "county": county_id or "",
            "sub_county": sub_county_id or "",
            "chu": chu_id or "",
            "area": area_id or "",
            "q": scope["search_query"],
            "view": scope["view_param"] or "",
        },
        "selected_county_name": selected_county_name,
        "selected_sub_county_name": selected_sub_county_name,
        "selected_area_name": selected_area_name,
        "s11": build_chp_s11_summary(scope["records"], scope["filtered_areas"], page_number=page_number),
    }
    return render(request, "core/chp_s11.html", context)


def _chp_csv_response(filename, header, rows):
    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    writer = csv.writer(response)
    writer.writerow(header)
    writer.writerows(rows)
    return response


@login_required
def export_chp_balances_csv(request):
    """CSV of Commodity Balances, same columns/order as the on-screen table."""
    scope = _chp_resolve_scope(request)
    rows = build_chp_balance_summary(scope["records"], period_range=scope["period_range"])
    header = [
        "Commodity",
        "Beginning Balance",
        "Received",
        "Stock in Hand",
        "Dispensed",
        "Ending Balance",
        "Avg Weeks of Stock",
        "Flagged Rows Excluded (Dispensed)",
    ]
    data = [
        [
            row["commodity"].label,
            row["beginning_balance"],
            row["quantity_received"],
            row["stock_on_hand"],
            row["quantity_dispensed"],
            row["ending_balance"],
            row["avg_weeks_of_stock"],
            row["flagged_rows_excluded"],
        ]
        for row in rows
    ]
    filename = f"chp-commodity-balances-{scope['range_key'] or scope['period'] or 'all'}.csv"
    return _chp_csv_response(filename, header, data)


@login_required
def export_chp_moh748_csv(request):
    """CSV of the Generated MOH 748 Preview — a preview export, same caveat as the on-screen card: not a real submission."""
    scope = _chp_resolve_scope(request)
    rows = build_chp_moh748_preview(scope["records"], period_range=scope["period_range"])
    header = [
        "Drug Name",
        "Available from eCHIS",
        "A. Beginning Balance",
        "B. Quantity Received",
        "C. Total Dispensed",
        "D. Losses (Excl. Expiries)",
        "E. Balance / Physical Count",
        "F. Est. Days Out of Stock",
        "Flagged Rows Excluded (Dispensed)",
    ]
    data = [
        [
            row["commodity_label"],
            "Yes" if row["available"] else "No",
            row["beginning_balance"],
            row["quantity_received"],
            row["total_dispensed"],
            row["losses_excl_expiries"],
            row["physical_count"],
            row["estimated_days_out_of_stock"],
            row["flagged_rows_excluded"],
        ]
        for row in rows
    ]
    filename = f"generated-moh748-preview-{scope['range_key'] or scope['period'] or 'all'}.csv"
    return _chp_csv_response(filename, header, data)


@login_required
def export_chp_s11_csv(request):
    """
    CSV of every CHU's draft S11 voucher in the current scope (not just the
    on-screen page of them) — Quantity Required is always 0, same caveat as
    the on-screen page: see _build_chp_s11_vouchers()'s docstring.
    """
    scope = _chp_resolve_scope(request)
    vouchers = _build_chp_s11_vouchers(scope["records"], scope["filtered_areas"])
    header = [
        "Community Health Unit",
        "Sub-County",
        "County",
        "Item Description",
        "Unit of Issue",
        "Quantity Required",
        "Quantity Issued",
    ]
    data = []
    for voucher in vouchers:
        chu = voucher["chu"]
        chu_name = chu.name if chu else "Unresolved CHU"
        sub_county_name = chu.sub_county.name if chu else ""
        county_name = chu.sub_county.county.name if chu else ""
        for row in voucher["rows"]:
            data.append(
                [
                    chu_name,
                    sub_county_name,
                    county_name,
                    row["commodity"].label,
                    row["unit_of_issue"],
                    0,
                    row["quantity_issued"] or 0,
                ]
            )
    filename = f"generated-s11-{scope['range_key'] or scope['period'] or 'all'}.csv"
    return _chp_csv_response(filename, header, data)


@login_required
def export_chp_stock_status_csv(request):
    """
    CSV of CHP Stock Status — every CHP in the current filter scope, not
    just whatever page the on-screen heatmap happens to be showing.
    """
    scope = _chp_resolve_scope(request)
    rows = _build_chp_heatmap_rows(scope["latest_records"])
    commodities = list(CHPCommodity)
    header = ["CHP", "County", "Sub-County", "Community Health Unit", "Last Received"] + [
        c.label for c in commodities
    ]
    data = []
    for row in rows:
        area = row["area"]
        chu = area.community_health_unit
        line = [
            area.name or area.external_id,
            area.county.name if area.county_id else "",
            chu.sub_county.name if chu else "",
            chu.name if chu else "",
            row["last_received_display"] or "",
        ]
        for cell in row["cells"]:
            if cell is None:
                line.append("")
            elif cell["stock_on_hand"] is not None:
                line.append(f'{cell["stock_on_hand"]} ({cell["full_label"]})')
            else:
                line.append(cell["full_label"])
        data.append(line)
    filename = f"chp-stock-status-{scope['period'] or 'all'}.csv"
    return _chp_csv_response(filename, header, data)


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
