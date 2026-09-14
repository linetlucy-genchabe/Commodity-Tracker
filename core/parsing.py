"""
MOH 748 Excel workbook parser.

Kept as its own file, separate from views.py, because it's a distinct ETL
concern (column classification, number cleaning, period normalisation) — not
a request/response concern. Everything else for this app lives in
models.py / views.py / urls.py / admin.py.
"""

import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

import pandas as pd
from django.db import transaction

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
    IndicatorSummary,
    MOH748Record,
    MOH748Upload,
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


def _excel_engine_for(filename):
    """
    DHIS2's own export is often a legacy .xls (Excel 97-2003 binary) file
    even when it looks like it should be .xlsx — pandas needs a different
    engine for each, and only raises the "install xlrd" ImportError once it
    hits the file, not before. Picking the engine from the filename up
    front means a legacy export just works instead of crashing with a
    message that reads like a broken install.
    """
    name = (filename or "").lower()
    if name.endswith(".xls"):
        return "xlrd"
    if name.endswith(".xlsx") or name.endswith(".xlsm"):
        return "openpyxl"
    return None  # let pandas guess for anything else (e.g. no extension)


@transaction.atomic
def parse_moh748_workbook(file_obj, *, source_filename, uploaded_by):
    engine = _excel_engine_for(source_filename)
    try:
        df = pd.read_excel(file_obj, sheet_name=0, header=1, engine=engine)
    except ImportError as exc:
        if "xlrd" in str(exc):
            raise ImportError(
                "This looks like a legacy .xls file (Excel 97-2003 format), which needs the "
                "'xlrd' package to read. Run `pip install xlrd` in your virtual environment, "
                "then upload again. (Re-saving the file as .xlsx from Excel and re-uploading "
                "works too, and avoids needing xlrd at all.)"
            ) from exc
        raise
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

    # period is set once every row has been read (a single export can span
    # many months — see the per-row period handling below) and saved at the
    # end alongside facility_rows_parsed/record_rows_created.
    upload = MOH748Upload.objects.create(
        period="",
        source_filename=source_filename,
        uploaded_by=uploaded_by,
    )

    # The threshold record is the same for every row and every commodity in
    # this import (it's keyed on form + metric_key, both constant) — look it
    # up once here instead of once per commodity per row. On a real export
    # with hundreds of facility rows × up to 5 commodities, that alone was
    # thousands of redundant round trips to the database.
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

    facility_rows_parsed = 0
    record_rows_created = 0
    warnings = []

    # Real DHIS2 exports repeat the same county/sub-county/ward across
    # dozens or hundreds of facility rows. get_or_create() still issues a
    # query every time even when nothing needs creating, so cache each
    # level in memory for the duration of this import — same end result in
    # the database, far fewer round trips for a large file.
    county_cache = {}
    sub_county_cache = {}
    ward_cache = {}
    facility_cache = {}

    # (facility_id, period, commodity) -> {facility, county, sub_county,
    # ward, period, fields} accumulated across the row loop and written to
    # the database in bulk afterwards — see the "Bulk-write everything
    # collected above" block. period is part of the key (not a single
    # value for the whole import) because one export commonly reports many
    # months at once — see the per-row period handling just below.
    touched = {}
    periods_seen = set()

    for _, row in df.iterrows():
        county_name = _strip_admin_suffix(row.get("orgunitlevel2", ""))
        sub_county_name = _strip_admin_suffix(row.get("orgunitlevel3", ""))
        ward_name = _strip_admin_suffix(row.get("orgunitlevel4", ""))
        facility_name = str(
            row.get("organisationunitname") or row.get("orgunitlevel5", "")
        ).strip()

        # Read the period from *this* row rather than once for the whole
        # file. A single DHIS2 export routinely bundles many months into
        # one sheet (one periodname value per row, not per file) — reading
        # only the first row's value and applying it to everything was why
        # a 12-month export was landing entirely under a single month.
        raw_period = row.get(PERIOD_COLUMN) if PERIOD_COLUMN in df.columns else None
        if raw_period is None or (isinstance(raw_period, float) and pd.isna(raw_period)):
            period = ""
        else:
            period = _normalize_period(raw_period)

        if not facility_name or facility_name.lower() == "nan":
            continue
        if not period:
            warnings.append(f"Skipped '{facility_name}': missing/unreadable period in source row.")
            continue
        if not county_name or not sub_county_name or not ward_name:
            warnings.append(f"Skipped '{facility_name}': incomplete org-unit hierarchy in source row.")
            continue

        periods_seen.add(period)

        county = county_cache.get(county_name)
        if county is None:
            county, _ = County.objects.get_or_create(name=county_name)
            county_cache[county_name] = county

        sub_county_key = (county.id, sub_county_name)
        sub_county = sub_county_cache.get(sub_county_key)
        if sub_county is None:
            sub_county, _ = SubCounty.objects.get_or_create(county=county, name=sub_county_name)
            sub_county_cache[sub_county_key] = sub_county

        ward_key = (sub_county.id, ward_name)
        ward = ward_cache.get(ward_key)
        if ward is None:
            ward, _ = Ward.objects.get_or_create(sub_county=sub_county, name=ward_name)
            ward_cache[ward_key] = ward

        facility_key = (ward.id, facility_name)
        facility = facility_cache.get(facility_key)
        if facility is None:
            facility, _ = Facility.objects.get_or_create(ward=ward, name=facility_name)
            facility_cache[facility_key] = facility

        facility_rows_parsed += 1

        by_commodity = {}
        for col, (commodity, field_name) in column_map.items():
            by_commodity.setdefault(commodity, {})[field_name] = _clean_number(row.get(col))

        for commodity, fields in by_commodity.items():
            if all(v is None for v in fields.values()):
                continue

            # Collect in memory instead of writing to the database here —
            # a real export runs this inner loop thousands of times (rows
            # × commodities), and issuing a handful of small queries on
            # every single iteration is what was making large uploads slow.
            # The dict key doubles as the dedup key: the same facility can
            # legitimately appear more than once in one DHIS2 export
            # (duplicate org-unit rows happen), and keeping the later row's
            # fields — same as the update_or_create this replaced — is
            # exactly what "last row in the file wins" should mean.
            touched[(facility.id, period, commodity)] = {
                "facility": facility,
                "county": county,
                "sub_county": sub_county,
                "ward": ward,
                "period": period,
                "fields": fields,
            }

    record_rows_created = len(touched)

    # --- Bulk-write everything collected above -----------------------------
    # Old records for a touched (facility, period, commodity) are always
    # superseded by this fresh upload. Grouping by (period, commodity) turns
    # what used to be one DELETE per facility per commodity per row into at
    # most (number of months) × 5 DELETEs total for the whole import.
    facility_ids_by_period_commodity = {}
    for facility_id, period, commodity in touched:
        facility_ids_by_period_commodity.setdefault((period, commodity), set()).add(facility_id)

    for (period, commodity), facility_ids in facility_ids_by_period_commodity.items():
        MOH748Record.objects.filter(
            period=period, commodity=commodity, facility_id__in=facility_ids
        ).exclude(upload=upload).delete()

    # Every record here belongs to the upload row created above — a brand
    # new, empty upload — so there's nothing existing to update against and
    # a single bulk_create covers the whole import (chunked automatically
    # into batches by Django).
    MOH748Record.objects.bulk_create(
        [
            MOH748Record(
                upload=upload,
                facility=data["facility"],
                period=period,
                commodity=commodity,
                **data["fields"],
            )
            for (facility_id, period, commodity), data in touched.items()
        ],
        batch_size=500,
    )

    # IndicatorSummary rows are keyed by (form, facility, period,
    # indicator_key, metric_key) — not by upload — so re-uploading a period
    # genuinely needs to update the *same* existing row rather than create a
    # new one. Fetch every row that could possibly match in one query, split
    # touched entries into updates vs. creates against that, then write each
    # group in one bulk call instead of one update_or_create per row.
    all_facility_ids = {facility_id for facility_id, _, _ in touched}
    existing_summaries = {
        (s.facility_id, s.period, s.indicator_key): s
        for s in IndicatorSummary.objects.filter(
            form=form, period__in=periods_seen, metric_key="days_out_of_stock", facility_id__in=all_facility_ids
        )
    }

    summaries_to_update = []
    summaries_to_create = []
    for (facility_id, period, commodity), data in touched.items():
        indicator_key = f"moh748_{commodity.lower()}"
        days_out = data["fields"].get("days_out_of_stock")
        status = threshold.status_for(days_out)
        label = f"{commodity.label} — Days Out of Stock"

        existing = existing_summaries.get((facility_id, period, indicator_key))
        if existing:
            existing.county = data["county"]
            existing.sub_county = data["sub_county"]
            existing.ward = data["ward"]
            existing.indicator_label = label
            existing.value = days_out
            existing.status = status
            summaries_to_update.append(existing)
        else:
            summaries_to_create.append(
                IndicatorSummary(
                    form=form,
                    county=data["county"],
                    sub_county=data["sub_county"],
                    ward=data["ward"],
                    facility=data["facility"],
                    period=period,
                    indicator_key=indicator_key,
                    indicator_label=label,
                    metric_key="days_out_of_stock",
                    value=days_out,
                    status=status,
                )
            )

    if summaries_to_update:
        IndicatorSummary.objects.bulk_update(
            summaries_to_update,
            fields=["county", "sub_county", "ward", "indicator_label", "value", "status"],
            batch_size=500,
        )
    if summaries_to_create:
        IndicatorSummary.objects.bulk_create(summaries_to_create, batch_size=500)

    # This upload's period label: the one period if the whole file was a
    # single month (unchanged from before), or a compact range if it
    # covered several — a 12-month export like this one no longer
    # collapses down to just its first month. MOH748Upload.period is a
    # CharField(max_length=20), so this has to stay short: "YYYY-MM–YYYY-MM"
    # is 15 characters no matter how many periods are in between, well
    # under the limit (an earlier version of this label included a
    # "(N periods)" suffix that overflowed the column on anything with
    # more than a handful of months).
    sorted_periods = sorted(periods_seen)
    if len(sorted_periods) <= 1:
        upload_period = sorted_periods[0] if sorted_periods else ""
    else:
        upload_period = f"{sorted_periods[0]}–{sorted_periods[-1]}"

    upload.period = upload_period
    upload.facility_rows_parsed = facility_rows_parsed
    upload.record_rows_created = record_rows_created
    upload.save(update_fields=["period", "facility_rows_parsed", "record_rows_created"])

    return ParseResult(
        upload=upload,
        facility_rows_parsed=facility_rows_parsed,
        record_rows_created=record_rows_created,
        unclassified_columns=unclassified_columns,
        warnings=warnings,
    )


# ---------------------------------------------------------------------------
# CHP Commodity Stock Flow — eCHIS export parser
#
# Reads the "CHP Commodity Detail" sheet of an eCHIS-sourced workbook. Unlike
# the DHIS2 MOH 748 export, this source already has clean, human-readable
# column headers, so there's no header-suffix classification step — columns
# map straight to model fields by name.
# ---------------------------------------------------------------------------

CHP_SHEET_NAME = "CHP Commodity Detail"

# Column -> model field, for the numeric stock-flow columns.
_CHP_DECIMAL_FIELDS = {
    "Beginning Balance": "beginning_balance",
    "Quantity Received": "quantity_received",
    "Quantity Dispensed": "quantity_dispensed",
    "Positive Adjustments": "positive_adjustments",
    "Negative Adjustments": "negative_adjustments",
    "Ending Balance": "ending_balance",
    "Physical Count": "physical_count",
    "Stock on Hand": "stock_on_hand",
    "Weeks of Stock": "weeks_of_stock",
}
_CHP_DATE_FIELDS = {
    "Latest Balance Date": "latest_balance_date",
    "Latest Consumption Date": "latest_consumption_date",
    "Latest Count Date": "latest_count_date",
    "Latest Stockout Date": "latest_stockout_date",
}
_CHP_COUNT_FIELDS = {
    "Consumption Log Count": "consumption_log_count",
    "Supply Form Count": "supply_form_count",
    "Count Form Count": "count_form_count",
    "Stockout Form Count": "stockout_form_count",
}

_CHP_STOCK_STATUS_VALUES = {c.value for c in CHPCommodityStockStatus}
_CHP_COMMODITY_CODES = {c.value for c in CHPCommodity}


def _clean_decimal(value):
    if value is None:
        return None
    if isinstance(value, float) and pd.isna(value):
        return None
    if isinstance(value, str) and not value.strip():
        return None
    try:
        return Decimal(str(value)).quantize(Decimal("0.01"))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _clean_int(value):
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


def _clean_date(value):
    if value is None:
        return None
    if isinstance(value, float) and pd.isna(value):
        return None
    try:
        parsed = pd.to_datetime(value)
    except (ValueError, TypeError):
        return None
    if pd.isna(parsed):
        return None
    return parsed.date()


def _clean_text(value):
    if value is None:
        return ""
    if isinstance(value, float) and pd.isna(value):
        return ""
    text = str(value).strip()
    return "" if text.lower() == "nan" else text


@dataclass
class CHPParseResult:
    upload: CHPCommodityUpload
    area_rows_parsed: int
    record_rows_created: int
    unresolved_geography_rows: int
    unmapped_chw_rows: int
    warnings: list = field(default_factory=list)


@transaction.atomic
def parse_chp_commodity_workbook(file_obj, *, source_filename, uploaded_by):
    engine = _excel_engine_for(source_filename)
    try:
        df = pd.read_excel(file_obj, sheet_name=CHP_SHEET_NAME, header=0, engine=engine)
    except ValueError as exc:
        # pandas raises ValueError once it *has* opened the workbook but
        # can't find the named sheet — the common "wrong file" mistake.
        raise ValueError(
            f"Couldn't find a '{CHP_SHEET_NAME}' sheet in this workbook. Make sure you're uploading "
            "the eCHIS CHP Commodity Stock Flow export, not a different file."
        ) from exc
    except Exception as exc:
        # Anything else — a corrupted download, a .csv renamed to .xlsx, a
        # non-Excel file entirely — fails lower down (openpyxl/zipfile) with
        # something that isn't a ValueError. Turn it into the same kind of
        # clean, on-page error instead of a raw server error.
        raise ValueError(
            "Couldn't read this file as an Excel workbook. Make sure you're uploading the eCHIS "
            "CHP Commodity Stock Flow export (.xlsx), not a different file type or a corrupted download."
        ) from exc
    df.columns = [str(c).strip() for c in df.columns]

    required_columns = {"CHP Area ID", "Commodity Code", "County", "Period Start"}
    missing = required_columns - set(df.columns)
    if missing:
        raise ValueError(
            f"This workbook's '{CHP_SHEET_NAME}' sheet is missing expected column(s): "
            f"{', '.join(sorted(missing))}."
        )

    # period is set once every row has been read — a single export could in
    # principle span more than one month, same reasoning as MOH 748's
    # per-row period handling below, even though a normal upload going
    # forward covers exactly one reporting month.
    upload = CHPCommodityUpload.objects.create(
        period="",
        source_filename=source_filename,
        uploaded_by=uploaded_by,
    )

    # Real exports repeat the same county/sub-county/CHU across every
    # commodity row for one CHP area (roughly 8 rows per area) — cache each
    # level so a large file doesn't issue a redundant query per row.
    county_cache = {}
    sub_county_cache = {}
    chu_cache = {}
    area_cache = {}  # external_id (CHP Area ID) -> CHPArea, for this run

    # (area.id, period, commodity_code) -> field dict, written in bulk after
    # the row loop — same pattern as MOH 748's parser.
    touched = {}
    periods_seen = set()
    warnings = []
    area_rows_parsed = 0

    # Counted the same way the source workbook's own Summary sheet counts
    # them — per detail row, not deduplicated per area — so these numbers
    # can be checked directly against "Rows with unresolved geography
    # labels" / "Rows without current active CHW mapping" on that sheet.
    unresolved_geography_rows = 0
    unmapped_chw_rows = 0

    for _, row in df.iterrows():
        external_id = _clean_text(row.get("CHP Area ID"))
        commodity_code = _clean_text(row.get("Commodity Code"))
        county_name = _clean_text(row.get("County"))

        if not external_id:
            warnings.append("Skipped a row with no CHP Area ID.")
            continue
        if not county_name:
            warnings.append(f"Skipped CHP area '{external_id}': no County given.")
            continue
        if commodity_code not in _CHP_COMMODITY_CODES:
            warnings.append(
                f"Skipped CHP area '{external_id}': unrecognised commodity code '{commodity_code}'."
            )
            continue

        raw_period = row.get("Period Start")
        if raw_period is None or (isinstance(raw_period, float) and pd.isna(raw_period)):
            warnings.append(f"Skipped CHP area '{external_id}': missing/unreadable Period Start.")
            continue
        period = _normalize_period(raw_period)
        if not period:
            warnings.append(f"Skipped CHP area '{external_id}': unreadable Period Start.")
            continue
        periods_seen.add(period)

        county = county_cache.get(county_name)
        if county is None:
            county, _ = County.objects.get_or_create(name=county_name)
            county_cache[county_name] = county

        sub_county_name = _clean_text(row.get("Sub-county"))
        chu_name = _clean_text(row.get("Community Health Unit"))

        community_health_unit = None
        if sub_county_name and chu_name:
            sub_county_key = (county.id, sub_county_name)
            sub_county = sub_county_cache.get(sub_county_key)
            if sub_county is None:
                sub_county, _ = SubCounty.objects.get_or_create(county=county, name=sub_county_name)
                sub_county_cache[sub_county_key] = sub_county

            chu_key = (sub_county.id, chu_name)
            community_health_unit = chu_cache.get(chu_key)
            if community_health_unit is None:
                community_health_unit, _ = CommunityHealthUnit.objects.get_or_create(
                    sub_county=sub_county, name=chu_name
                )
                chu_cache[chu_key] = community_health_unit
        else:
            # Kept, not dropped — see CHPArea.community_health_unit's
            # docstring. This still gets a real, county-scoped row.
            unresolved_geography_rows += 1

        chw_mapping_status = _clean_text(row.get("CHW Mapping Status"))
        if "no current active chw" in chw_mapping_status.lower():
            unmapped_chw_rows += 1

        area = area_cache.get(external_id)
        if area is None:
            area, _ = CHPArea.objects.update_or_create(
                external_id=external_id,
                defaults={
                    "county": county,
                    "community_health_unit": community_health_unit,
                    "name": _clean_text(row.get("CHP Area"))[:200],
                    "chw_count": _clean_int(row.get("CHW Count")),
                    "chw_name": _clean_text(row.get("CHW Name(s)"))[:255],
                    "chw_username": _clean_text(row.get("CHW Username(s)"))[:255],
                    "chw_contact_id": _clean_text(row.get("CHW Contact ID(s)"))[:255],
                    "chw_mapping_status": chw_mapping_status[:100],
                    "geography_status": _clean_text(row.get("Geography Status"))[:100],
                },
            )
            area_cache[external_id] = area
        area_rows_parsed += 1

        fields = {}
        for col, field_name in _CHP_DECIMAL_FIELDS.items():
            fields[field_name] = _clean_decimal(row.get(col))
        for col, field_name in _CHP_DATE_FIELDS.items():
            fields[field_name] = _clean_date(row.get(col))
        for col, field_name in _CHP_COUNT_FIELDS.items():
            fields[field_name] = _clean_int(row.get(col))

        stock_status_raw = _clean_text(row.get("Stock Status"))
        fields["stock_status"] = stock_status_raw if stock_status_raw in _CHP_STOCK_STATUS_VALUES else ""
        fields["service_qty_review_flag"] = _clean_text(row.get("Service Qty Review Flag"))[:255]

        touched[(area.id, period, commodity_code)] = fields

    record_rows_created = len(touched)

    # --- Bulk-write everything collected above -----------------------------
    area_ids_by_period_commodity = {}
    for area_id, period, commodity in touched:
        area_ids_by_period_commodity.setdefault((period, commodity), set()).add(area_id)

    for (period, commodity), area_ids in area_ids_by_period_commodity.items():
        CHPCommodityRecord.objects.filter(
            period=period, commodity=commodity, chp_area_id__in=area_ids
        ).exclude(upload=upload).delete()

    CHPCommodityRecord.objects.bulk_create(
        [
            CHPCommodityRecord(upload=upload, chp_area_id=area_id, period=period, commodity=commodity, **fields)
            for (area_id, period, commodity), fields in touched.items()
        ],
        batch_size=500,
    )

    sorted_periods = sorted(periods_seen)
    if len(sorted_periods) <= 1:
        upload_period = sorted_periods[0] if sorted_periods else ""
    else:
        upload_period = f"{sorted_periods[0]}–{sorted_periods[-1]}"

    upload.period = upload_period
    upload.area_rows_parsed = area_rows_parsed
    upload.record_rows_created = record_rows_created
    upload.unresolved_geography_rows = unresolved_geography_rows
    upload.unmapped_chw_rows = unmapped_chw_rows
    upload.save(
        update_fields=[
            "period",
            "area_rows_parsed",
            "record_rows_created",
            "unresolved_geography_rows",
            "unmapped_chw_rows",
        ]
    )

    return CHPParseResult(
        upload=upload,
        area_rows_parsed=area_rows_parsed,
        record_rows_created=record_rows_created,
        unresolved_geography_rows=unresolved_geography_rows,
        unmapped_chw_rows=unmapped_chw_rows,
        warnings=warnings,
    )
