"""
MOH 748 Excel workbook parser.

Kept as its own file, separate from views.py, because it's a distinct ETL
concern (column classification, number cleaning, period normalisation) — not
a request/response concern. Everything else for this app lives in
models.py / views.py / urls.py / admin.py.
"""

import re
from dataclasses import dataclass, field

import pandas as pd
from django.db import transaction

from .models import (
    Commodity,
    County,
    Facility,
    FormDefinition,
    IndicatorSummary,
    MOH748Record,
    MOH748Upload,
    Status,
    SubCounty,
    Threshold,
    Ward,
)

# The source is a DHIS2-style wide export: org-unit hierarchy columns
# (orgunitlevel1..5 + organisationunitname), a period column, then repeating
# per-commodity blocks of metric columns. orgunitlevel1 is always "Kenya" and
# is ignored; orgunitlevel2..4 are County/Sub County/Ward, orgunitlevel5 (same
# as organisationunitname) is the facility.
ORG_UNIT_COLUMNS = [
    "orgunitlevel1",
    "orgunitlevel2",
    "orgunitlevel3",
    "orgunitlevel4",
    "orgunitlevel5",
    "organisationunitname",
]
PERIOD_COLUMN = "periodname"

# Longest first — "Sub County" also ends with "County", so the longer form
# must be tried before the shorter one or "Seme Sub County" strips down to
# "Seme Sub" instead of "Seme".
_ADMIN_SUFFIXES = sorted([" County", " Sub County", " SubCounty", " Ward"], key=len, reverse=True)

_PREFIX_RE = re.compile(r"^MOH\s*743\s*Rev\s*2020[_\s]*", re.IGNORECASE)

# Header suffix -> model field name. Longest-first matching handles the
# "Postive Adjustments." typo present in the real source workbooks, and
# tolerates trailing periods some columns have and others don't (the "6s"
# commodity block in real exports is often incomplete, missing the columns
# from "Postive Adjustments." onward — those rows still parse fine for
# whichever metrics are present).
_METRIC_SUFFIXES = {
    "Beginning Balance.": "beginning_balance",
    "Beginning Balance": "beginning_balance",
    "Days out of stock.": "days_out_of_stock",
    "Days out of stock": "days_out_of_stock",
    "Losses (Excluding Expiries).": "losses_excl_expiries",
    "Losses (Excluding Expiries)": "losses_excl_expiries",
    "Medicines with 6 months to Expiry.": "near_expiry_6mo",
    "Medicines with 6 months to Expiry": "near_expiry_6mo",
    "Negative Adjustments.": "negative_adjustments",
    "Negative Adjustments": "negative_adjustments",
    "Physical Count.": "physical_count",
    "Physical Count": "physical_count",
    "Postive Adjustments.": "positive_adjustments",
    "Postive Adjustments": "positive_adjustments",
    "Positive Adjustments.": "positive_adjustments",
    "Positive Adjustments": "positive_adjustments",
    "Quantity Received this period.": "quantity_received",
    "Quantity Received this period": "quantity_received",
    "Quantity Requested for Resupply.": "quantity_requested_resupply",
    "Quantity Requested for Resupply": "quantity_requested_resupply",
    "Quantity of Expired Drugs.": "quantity_expired",
    "Quantity of Expired Drugs": "quantity_expired",
    "Total Quantity dispensed.": "total_dispensed",
    "Total Quantity dispensed": "total_dispensed",
}

_SUFFIXES_BY_LENGTH = sorted(_METRIC_SUFFIXES, key=len, reverse=True)


@dataclass
class ParseResult:
    upload: MOH748Upload
    facility_rows_parsed: int
    record_rows_created: int
    unclassified_columns: list = field(default_factory=list)
    warnings: list = field(default_factory=list)


def _classify_commodity(header):
    lowered = header.lower()
    if "llin" in lowered:
        return Commodity.LLINS
    if "24s" in lowered:
        return Commodity.AL_24
    if "18s" in lowered:
        return Commodity.AL_18
    if "12s" in lowered:
        return Commodity.AL_12
    if re.search(r"\b6s\b", lowered):
        return Commodity.AL_6
    return None


def _strip_admin_suffix(name):
    if name is None or (isinstance(name, float) and pd.isna(name)):
        return ""
    name = str(name).strip()
    if name.lower() == "nan":
        return ""
    for suffix in _ADMIN_SUFFIXES:
        if name.endswith(suffix):
            return name[: -len(suffix)].strip()
    return name


def _classify_column(header):
    """Return (commodity, field_name) for a data column header, or None."""
    stripped = _PREFIX_RE.sub("", header).strip()
    for suffix in _SUFFIXES_BY_LENGTH:
        if stripped.endswith(suffix):
            commodity_part = stripped[: -len(suffix)].strip(" ._-")
            commodity = _classify_commodity(commodity_part)
            if commodity:
                return commodity, _METRIC_SUFFIXES[suffix]
    return None


def _clean_number(value):
    if value is None:
        return None
    if isinstance(value, float) and pd.isna(value):
        return None
    if isinstance(value, str) and not value.strip():
        return None
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return None


def _normalize_period(raw):
    if raw is None:
        return ""
    text = str(raw).strip()
    try:
        parsed = pd.to_datetime(text)
        return parsed.strftime("%Y-%m")
    except (ValueError, TypeError):
        return text


@transaction.atomic
def parse_moh748_workbook(file_obj, *, source_filename, uploaded_by):
    df = pd.read_excel(file_obj, sheet_name=0, header=1)
    df.columns = [str(c).strip() for c in df.columns]

    column_map = {}
    unclassified_columns = []
    for col in df.columns:
        if col in ORG_UNIT_COLUMNS or col == PERIOD_COLUMN:
            continue
        classified = _classify_column(col)
        if classified:
            column_map[col] = classified
        else:
            unclassified_columns.append(col)

    period = ""
    if PERIOD_COLUMN in df.columns and not df[PERIOD_COLUMN].dropna().empty:
        period = _normalize_period(df[PERIOD_COLUMN].dropna().iloc[0])

    form, _ = FormDefinition.objects.get_or_create(
        slug="moh748",
        defaults={
            "name": "MOH 748 — Commodity Stock Status",
            "short_name": "MOH 748",
            "category": "Commodity",
            "is_active": True,
            "display_order": 1,
        },
    )

    upload = MOH748Upload.objects.create(
        period=period,
        source_filename=source_filename,
        uploaded_by=uploaded_by,
    )

    facility_rows_parsed = 0
    record_rows_created = 0
    warnings = []

    for _, row in df.iterrows():
        county_name = _strip_admin_suffix(row.get("orgunitlevel2", ""))
        sub_county_name = _strip_admin_suffix(row.get("orgunitlevel3", ""))
        ward_name = _strip_admin_suffix(row.get("orgunitlevel4", ""))
        facility_name = str(
            row.get("organisationunitname") or row.get("orgunitlevel5", "")
        ).strip()

        if not facility_name or facility_name.lower() == "nan":
            continue
        if not county_name or not sub_county_name or not ward_name:
            warnings.append(f"Skipped '{facility_name}': incomplete org-unit hierarchy in source row.")
            continue

        county, _ = County.objects.get_or_create(name=county_name)
        sub_county, _ = SubCounty.objects.get_or_create(county=county, name=sub_county_name)
        ward, _ = Ward.objects.get_or_create(sub_county=sub_county, name=ward_name)
        facility, _ = Facility.objects.get_or_create(ward=ward, name=facility_name)

        facility_rows_parsed += 1

        by_commodity = {}
        for col, (commodity, field_name) in column_map.items():
            by_commodity.setdefault(commodity, {})[field_name] = _clean_number(row.get(col))

        for commodity, fields in by_commodity.items():
            if all(v is None for v in fields.values()):
                continue

            MOH748Record.objects.filter(
                facility=facility, period=period, commodity=commodity
            ).exclude(upload=upload).delete()

            record = MOH748Record.objects.create(
                upload=upload,
                facility=facility,
                period=period,
                commodity=commodity,
                **fields,
            )
            record_rows_created += 1

            threshold, _ = Threshold.objects.get_or_create(
                form=form,
                metric_key="days_out_of_stock",
                defaults={
                    "label": "Days Out of Stock",
                    "unit": "days",
                    "green_max": 0,
                    "amber_max": 6,
                    "is_draft": True,
                    "definition": "Draft assumption pending confirmed MOH thresholds.",
                },
            )
            status = threshold.status_for(record.days_out_of_stock)

            IndicatorSummary.objects.update_or_create(
                form=form,
                facility=facility,
                period=period,
                indicator_key=f"moh748_{commodity.lower()}",
                metric_key="days_out_of_stock",
                defaults={
                    "county": county,
                    "sub_county": sub_county,
                    "ward": ward,
                    "indicator_label": f"{record.get_commodity_display()} — Days Out of Stock",
                    "value": record.days_out_of_stock,
                    "status": status,
                },
            )

    upload.facility_rows_parsed = facility_rows_parsed
    upload.record_rows_created = record_rows_created
    upload.save(update_fields=["facility_rows_parsed", "record_rows_created"])

    return ParseResult(
        upload=upload,
        facility_rows_parsed=facility_rows_parsed,
        record_rows_created=record_rows_created,
        unclassified_columns=unclassified_columns,
        warnings=warnings,
    )
