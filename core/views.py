"""
All views for the Commodity Tracker, plus the query-helper functions that
build dashboard context and the tiny upload form. Kept in one file — see
README "Project layout".
"""

import json

from django import forms
from django.contrib.auth.decorators import login_required, user_passes_test
from django.core.paginator import Paginator
from django.core.serializers.json import DjangoJSONEncoder
from django.db.models import Avg, Count, Sum
from django.shortcuts import get_object_or_404, redirect, render

from .models import (
    Commodity,
    County,
    Facility,
    FormDefinition,
    MOH748Record,
    Status,
    SubCounty,
    Threshold,
    Ward,
)
from .parsing import parse_moh748_workbook

PAGE_SIZE = 25
TREND_MONTHS = 8


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

    return {
        "total_in_scope": total_in_scope,
        "reporting": reporting,
        "with_stockout": with_stockout,
        "with_stockout_pct": with_stockout_pct,
        "avg_days": avg_days,
        "near_expiry": near_expiry,
    }


def build_heatmap(records_qs, *, page_number=1):
    facility_ids = list(records_qs.values_list("facility_id", flat=True).distinct())
    facilities = list(
        Facility.objects.filter(id__in=facility_ids)
        .select_related("ward__sub_county__county")
        .order_by("name")
    )

    paginator = Paginator(facilities, PAGE_SIZE)
    page_obj = paginator.get_page(page_number)

    page_facility_ids = [f.id for f in page_obj.object_list]
    records = records_qs.filter(facility_id__in=page_facility_ids)
    by_facility_commodity = {(r.facility_id, r.commodity): r for r in records}

    rows = []
    for facility in page_obj.object_list:
        cells = []
        for commodity in Commodity:
            record = by_facility_commodity.get((facility.id, commodity))
            if record is None:
                cells.append(None)
                continue
            status = None
            if record.days_out_of_stock is not None:
                if record.days_out_of_stock <= 0:
                    status = Status.GREEN
                elif record.days_out_of_stock <= 6:
                    status = Status.AMBER
                else:
                    status = Status.RED
            cells.append({"value": record.days_out_of_stock, "status": status})
        rows.append({"facility": facility, "cells": cells})

    return {
        "rows": rows,
        "commodities": [(c.value, c.label) for c in Commodity],
        "page": page_obj,
    }


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
# Upload form (formerly moh748/forms.py)
# ---------------------------------------------------------------------------


class MOH748UploadForm(forms.Form):
    file = forms.FileField(label="MOH 748 workbook (.xlsx)")


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

    filtered_facilities = apply_geo_filters(
        facilities, county_id=county_id, sub_county_id=sub_county_id, ward_id=ward_id
    )
    if facility_id:
        filtered_facilities = filtered_facilities.filter(id=facility_id)

    records = MOH748Record.objects.filter(period=period, facility__in=filtered_facilities).select_related(
        "facility__ward__sub_county__county"
    )

    try:
        page_number = int(request.GET.get("page", 1))
    except ValueError:
        page_number = 1

    trend_qs = MOH748Record.objects.filter(facility__in=filtered_facilities)

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
        },
        "kpis": build_kpis(records, filtered_facilities),
        "heatmap": build_heatmap(records, page_number=page_number),
        "top_facilities": top_stockout_facilities(records),
        "trend_json": json.dumps(trend_series(trend_qs), cls=DjangoJSONEncoder),
        "trend_months": TREND_MONTHS,
        "can_upload": user.may_upload,
    }
    return render(request, "core/home.html", context)


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
    for record in records:
        status = None
        if record.days_out_of_stock is not None:
            if threshold:
                status = threshold.status_for(record.days_out_of_stock)
            if record.days_out_of_stock > 0:
                stockout_count += 1
        rows.append({"record": record, "status": status})

    context = {
        "facility": facility,
        "periods": periods,
        "period": period,
        "period_display": period_display(period),
        "threshold": threshold,
        "rows": rows,
        "stockout_count": stockout_count,
    }
    return render(request, "core/facility_drilldown.html", context)


def _can_upload(user):
    return user.is_authenticated and user.may_upload


@login_required
@user_passes_test(_can_upload, login_url="home")
def upload_moh748(request):
    result = None
    if request.method == "POST":
        form = MOH748UploadForm(request.POST, request.FILES)
        if form.is_valid():
            uploaded = request.FILES["file"]
            result = parse_moh748_workbook(
                uploaded,
                source_filename=uploaded.name,
                uploaded_by=request.user,
            )
            return redirect("home")
    else:
        form = MOH748UploadForm()

    return render(request, "core/upload.html", {"form": form, "result": result})
