"""
All Commodity Tracker models in one place: geography, accounts, the forms
registry (with the shared cross-form IndicatorSummary table), and the MOH 748
upload/record tables.

Kept as one file on purpose — see README "Project layout" for why.
"""

from django.contrib.auth.models import AbstractUser
from django.db import models


# ---------------------------------------------------------------------------
# Geography — County > SubCounty > Ward > Facility
# ---------------------------------------------------------------------------


class County(models.Model):
    name = models.CharField(max_length=100, unique=True)

    class Meta:
        verbose_name_plural = "Counties"
        ordering = ["name"]

    def __str__(self):
        return self.name


class SubCounty(models.Model):
    county = models.ForeignKey(County, on_delete=models.PROTECT, related_name="sub_counties")
    name = models.CharField(max_length=100)

    class Meta:
        verbose_name_plural = "Sub-counties"
        unique_together = ("county", "name")
        ordering = ["county__name", "name"]

    def __str__(self):
        return f"{self.name} ({self.county.name})"


class Ward(models.Model):
    sub_county = models.ForeignKey(SubCounty, on_delete=models.PROTECT, related_name="wards")
    name = models.CharField(max_length=100)

    class Meta:
        verbose_name_plural = "Wards"
        unique_together = ("sub_county", "name")
        ordering = ["sub_county__county__name", "sub_county__name", "name"]

    def __str__(self):
        return f"{self.name} ({self.sub_county.name})"

    @property
    def county(self):
        return self.sub_county.county


class Facility(models.Model):
    ward = models.ForeignKey(Ward, on_delete=models.PROTECT, related_name="facilities")
    name = models.CharField(max_length=200)
    code = models.CharField(max_length=50, blank=True)

    class Meta:
        verbose_name_plural = "Facilities"
        unique_together = ("ward", "name")
        ordering = ["name"]

    def __str__(self):
        return self.name

    @property
    def sub_county(self):
        return self.ward.sub_county

    @property
    def county(self):
        return self.ward.sub_county.county


# ---------------------------------------------------------------------------
# Community geography — Community Health Unit > CHP Area
#
# A second branch under Sub-county, separate from Ward > Facility. eCHIS
# (the community-level data system) organizes its own reporting by Community
# Health Unit (CHU) and CHP Area (the patch covered by one community health
# promoter) — neither has any relationship to Ward or Facility in the data
# Lynne's been sent so far. `CommunityHealthUnit.facility` starts out blank
# and gets linked once she supplies the CHU-to-facility mapping; from that
# point on a CHU's numbers can roll up into the existing Facility/Sub-county
# views too, not just this branch.
# ---------------------------------------------------------------------------


class CommunityHealthUnit(models.Model):
    sub_county = models.ForeignKey(SubCounty, on_delete=models.PROTECT, related_name="community_health_units")
    name = models.CharField(max_length=200)
    facility = models.ForeignKey(
        Facility,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="community_health_units",
        help_text="Which facility this CHU reports through. Left blank until a CHU-to-facility mapping is supplied.",
    )

    class Meta:
        verbose_name_plural = "Community health units"
        unique_together = ("sub_county", "name")
        ordering = ["sub_county__county__name", "sub_county__name", "name"]

    def __str__(self):
        return f"{self.name} ({self.sub_county.name})"

    @property
    def county(self):
        return self.sub_county.county


class CHPArea(models.Model):
    """
    The smallest eCHIS reporting unit. `external_id` is eCHIS's own CHP Area
    ID, kept so re-uploads (or a later monthly upload) match the same area
    instead of creating a duplicate.

    community_health_unit is nullable on purpose: the source data includes
    rows with an "unresolved historical/orphan area ID" — no CHU, no area
    name, only a county and the raw ID. Those rows are kept, not dropped —
    same principle as MOH 748's facility list: a row with data never
    disappears just because part of its geography couldn't be resolved.

    chw_name / chw_username / chw_contact_id / chw_mapping_status exist for
    internal data-quality use only (e.g. counting areas with no currently
    mapped CHW) — never render these in any template. Lynne was explicit
    that community health worker identity should never reach a dashboard
    view, at any role.
    """

    county = models.ForeignKey(County, on_delete=models.PROTECT, related_name="chp_areas")
    community_health_unit = models.ForeignKey(
        CommunityHealthUnit, on_delete=models.SET_NULL, null=True, blank=True, related_name="chp_areas"
    )
    external_id = models.CharField(max_length=100, unique=True)
    name = models.CharField(max_length=200, blank=True)

    chw_count = models.PositiveIntegerField(null=True, blank=True)
    chw_name = models.CharField(max_length=255, blank=True)
    chw_username = models.CharField(max_length=255, blank=True)
    chw_contact_id = models.CharField(max_length=255, blank=True)
    chw_mapping_status = models.CharField(max_length=100, blank=True)
    geography_status = models.CharField(max_length=100, blank=True)

    class Meta:
        verbose_name_plural = "CHP areas"
        ordering = ["county__name", "name"]

    def __str__(self):
        return self.name or f"Unresolved area ({self.external_id})"

    @property
    def is_resolved(self):
        return self.community_health_unit_id is not None


# ---------------------------------------------------------------------------
# Accounts — roles, geography-scoped custom user
# ---------------------------------------------------------------------------


class Role(models.TextChoices):
    CHMT = "CHMT", "County Health Management Team"
    SCHMT = "SCHMT", "Sub-County Health Management Team"
    FACILITY_INCHARGE = "FACILITY_INCHARGE", "Facility In-charge"
    MEL_LEAD = "MEL_LEAD", "MEL Lead"


class User(AbstractUser):
    role = models.CharField(max_length=30, choices=Role.choices, blank=True)

    county = models.ForeignKey(County, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    sub_county = models.ForeignKey(SubCounty, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    facility = models.ForeignKey(Facility, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")

    can_upload = models.BooleanField(
        default=False,
        help_text="Allowed to upload MOH 748 (and future form) workbooks.",
    )

    def save(self, *args, **kwargs):
        if self.role == Role.MEL_LEAD:
            self.can_upload = True
        super().save(*args, **kwargs)

    @property
    def is_scoped(self):
        """True if this account should only see a slice of the geography."""
        if self.is_superuser:
            return False
        return self.role != Role.MEL_LEAD

    @property
    def may_upload(self):
        return self.is_superuser or self.can_upload

    def facility_queryset(self):
        """
        Facilities this user is allowed to see, based on role + assigned
        geography. Fails closed: a scoped role with no geography assigned
        sees nothing rather than everything.
        """
        if not self.is_scoped:
            return Facility.objects.all()

        if self.role == Role.FACILITY_INCHARGE:
            if self.facility_id:
                return Facility.objects.filter(pk=self.facility_id)
            return Facility.objects.none()

        if self.role == Role.SCHMT:
            if self.sub_county_id:
                return Facility.objects.filter(ward__sub_county_id=self.sub_county_id)
            return Facility.objects.none()

        if self.role == Role.CHMT:
            if self.county_id:
                return Facility.objects.filter(ward__sub_county__county_id=self.county_id)
            return Facility.objects.none()

        return Facility.objects.none()


# ---------------------------------------------------------------------------
# Forms registry — which MOH forms exist, thresholds, and the shared
# cross-form IndicatorSummary table used for filtering/highlighting.
# ---------------------------------------------------------------------------


class FormDefinition(models.Model):
    slug = models.SlugField(max_length=50, unique=True)
    name = models.CharField(max_length=150)
    short_name = models.CharField(max_length=30)
    category = models.CharField(max_length=50, blank=True)
    is_active = models.BooleanField(default=False)
    display_order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["display_order", "name"]

    def __str__(self):
        return self.short_name or self.name


class Status(models.TextChoices):
    GREEN = "GREEN", "Green"
    AMBER = "AMBER", "Amber"
    RED = "RED", "Red"


class Threshold(models.Model):
    form = models.ForeignKey(FormDefinition, on_delete=models.CASCADE, related_name="thresholds", null=True, blank=True)
    metric_key = models.CharField(max_length=100)
    label = models.CharField(max_length=150)
    unit = models.CharField(max_length=30, blank=True)

    green_max = models.DecimalField(max_digits=10, decimal_places=2)
    amber_max = models.DecimalField(max_digits=10, decimal_places=2)

    is_draft = models.BooleanField(
        default=True,
        help_text="Draft threshold — set from an assumption, not yet confirmed by MOH.",
    )
    definition = models.TextField(blank=True)

    class Meta:
        unique_together = ("form", "metric_key")
        ordering = ["form__display_order", "metric_key"]

    def __str__(self):
        draft = " (draft)" if self.is_draft else ""
        return f"{self.form}: {self.label}{draft}"

    def status_for(self, value):
        if value is None:
            return None
        if value <= self.green_max:
            return Status.GREEN
        if value <= self.amber_max:
            return Status.AMBER
        return Status.RED


class IndicatorSummary(models.Model):
    """
    One row per facility/period/indicator, regardless of which form it came
    from. Lets the dashboard filter and highlight across forms without a
    single universal fact table for the raw records themselves.
    """

    form = models.ForeignKey(FormDefinition, on_delete=models.CASCADE, related_name="indicator_summaries")

    county = models.ForeignKey(County, on_delete=models.CASCADE, related_name="+")
    sub_county = models.ForeignKey(SubCounty, on_delete=models.CASCADE, related_name="+")
    ward = models.ForeignKey(Ward, on_delete=models.CASCADE, related_name="+")
    facility = models.ForeignKey(Facility, on_delete=models.CASCADE, related_name="indicator_summaries")

    period = models.CharField(max_length=20)

    indicator_key = models.CharField(max_length=100)
    indicator_label = models.CharField(max_length=150)
    metric_key = models.CharField(max_length=100)

    value = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, null=True, blank=True)

    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name_plural = "Indicator summaries"
        unique_together = ("form", "facility", "period", "indicator_key", "metric_key")
        ordering = ["-period", "facility__name"]

    def __str__(self):
        return f"{self.facility} · {self.period} · {self.indicator_label}"


# ---------------------------------------------------------------------------
# MOH 748 — commodity stock uploads and records
# ---------------------------------------------------------------------------


class Commodity(models.TextChoices):
    LLINS = "LLINS", "LLINs"
    AL_6 = "AL_6", "AL 6s"
    AL_12 = "AL_12", "AL 12s"
    AL_18 = "AL_18", "AL 18s"
    AL_24 = "AL_24", "AL 24s"


class MOH748Upload(models.Model):
    period = models.CharField(max_length=20)
    source_filename = models.CharField(max_length=255)
    uploaded_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, related_name="moh748_uploads")
    uploaded_at = models.DateTimeField(auto_now_add=True)

    facility_rows_parsed = models.PositiveIntegerField(default=0)
    record_rows_created = models.PositiveIntegerField(default=0)
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["-uploaded_at"]

    def __str__(self):
        return f"MOH 748 · {self.period} · {self.source_filename}"


class MOH748Record(models.Model):
    upload = models.ForeignKey(MOH748Upload, on_delete=models.CASCADE, related_name="records")
    facility = models.ForeignKey(Facility, on_delete=models.CASCADE, related_name="moh748_records")
    period = models.CharField(max_length=20)
    commodity = models.CharField(max_length=10, choices=Commodity.choices)

    beginning_balance = models.IntegerField(null=True, blank=True)
    days_out_of_stock = models.IntegerField(null=True, blank=True)
    losses_excl_expiries = models.IntegerField(null=True, blank=True)
    near_expiry_6mo = models.IntegerField(null=True, blank=True)
    negative_adjustments = models.IntegerField(null=True, blank=True)
    physical_count = models.IntegerField(null=True, blank=True)
    positive_adjustments = models.IntegerField(null=True, blank=True)
    quantity_received = models.IntegerField(null=True, blank=True)
    quantity_requested_resupply = models.IntegerField(null=True, blank=True)
    quantity_expired = models.IntegerField(null=True, blank=True)
    total_dispensed = models.IntegerField(null=True, blank=True)

    class Meta:
        unique_together = ("facility", "period", "commodity", "upload")
        ordering = ["-period", "facility__name", "commodity"]

    def __str__(self):
        return f"{self.facility} · {self.period} · {self.get_commodity_display()}"


# ---------------------------------------------------------------------------
# CHP Commodity Stock Flow — eCHIS-sourced commodity data at CHP-area level
#
# A separate module from MOH 748, not an extension of it: this tracks a
# different reporting unit (CHP Area, not Facility) and a mostly different
# commodity set. Kept as its own upload/record pair, same shape as MOH748
# Upload/Record, so it follows the same monthly-upload pattern Lynne already
# knows from 748.
# ---------------------------------------------------------------------------


class CHPCommodity(models.TextChoices):
    """
    Deliberately separate from Commodity (MOH 748's enum): 748 tracks LLINs
    + AL only, while eCHIS also reports RDTs/Amoxicillin/ORS/Zinc. Keeping
    them apart means a CHP-only commodity never shows up in a 748 filter
    where it could never have data. Values match eCHIS's own commodity
    codes, so parsing is a direct lookup rather than a name guess.
    """

    AL_6 = "act_6", "AL 6s"
    AL_12 = "act_12", "AL 12s"
    AL_18 = "act_18", "AL 18s"
    AL_24 = "act_24", "AL 24s"
    RDTS = "rdts", "RDTs (Malaria Test Kit)"
    AMOXICILLIN_DT250 = "dt_250", "Amoxicillin 250mg DT"
    ORS_ZINC = "ors_zinc", "Zinc / ORS Co-Pack"
    ORS_SACHETS = "ors_sachets", "ORS Sachets"


class CHPCommodityStockStatus(models.TextChoices):
    STOCKOUT = "Stockout", "Stockout"
    LOW_STOCK = "Low stock", "Low stock"
    ADEQUATE = "Adequate", "Adequate"


class CHPCommodityUpload(models.Model):
    period = models.CharField(max_length=20, help_text="Reporting month this upload covers, e.g. 2026-08.")
    source_filename = models.CharField(max_length=255)
    uploaded_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, related_name="chp_commodity_uploads"
    )
    uploaded_at = models.DateTimeField(auto_now_add=True)

    area_rows_parsed = models.PositiveIntegerField(default=0)
    record_rows_created = models.PositiveIntegerField(default=0)
    unresolved_geography_rows = models.PositiveIntegerField(
        default=0, help_text="Rows kept under an 'Unresolved' bucket because eCHIS couldn't resolve their CHU/area."
    )
    unmapped_chw_rows = models.PositiveIntegerField(
        default=0, help_text="Rows for a CHP area with no currently active CHW mapped to it."
    )
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["-uploaded_at"]

    def __str__(self):
        return f"CHP Commodity Stock Flow · {self.period} · {self.source_filename}"


class CHPCommodityRecord(models.Model):
    upload = models.ForeignKey(CHPCommodityUpload, on_delete=models.CASCADE, related_name="records")
    chp_area = models.ForeignKey(CHPArea, on_delete=models.CASCADE, related_name="commodity_records")
    period = models.CharField(max_length=20)
    commodity = models.CharField(max_length=20, choices=CHPCommodity.choices)

    beginning_balance = models.DecimalField(max_digits=20, decimal_places=2, null=True, blank=True)
    quantity_received = models.DecimalField(max_digits=20, decimal_places=2, null=True, blank=True)
    quantity_dispensed = models.DecimalField(max_digits=20, decimal_places=2, null=True, blank=True)
    # Requested by Lynne but always blank at this reporting grain — eCHIS
    # has separate stock-amendment/discrepancy events instead, not a direct
    # positive/negative field here. Kept so the shape matches if that
    # changes later; see parsing.py / the Data Dictionary sheet.
    positive_adjustments = models.DecimalField(max_digits=20, decimal_places=2, null=True, blank=True)
    negative_adjustments = models.DecimalField(max_digits=20, decimal_places=2, null=True, blank=True)
    ending_balance = models.DecimalField(max_digits=20, decimal_places=2, null=True, blank=True)
    latest_balance_date = models.DateField(null=True, blank=True)

    consumption_log_count = models.PositiveIntegerField(null=True, blank=True)
    latest_consumption_date = models.DateField(null=True, blank=True)
    supply_form_count = models.PositiveIntegerField(null=True, blank=True)
    physical_count = models.DecimalField(max_digits=20, decimal_places=2, null=True, blank=True)
    count_form_count = models.PositiveIntegerField(null=True, blank=True)
    latest_count_date = models.DateField(null=True, blank=True)

    stock_on_hand = models.DecimalField(max_digits=20, decimal_places=2, null=True, blank=True)
    weeks_of_stock = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)
    stock_status = models.CharField(max_length=20, choices=CHPCommodityStockStatus.choices, blank=True)
    stockout_form_count = models.PositiveIntegerField(null=True, blank=True)
    latest_stockout_date = models.DateField(null=True, blank=True)

    # A short free-text note from the source extract flagging an
    # implausible raw quantity for manual review (e.g. "Review: raw total >
    # 10,000 units") — not a simple yes/no, so kept as text rather than a
    # boolean.
    service_qty_review_flag = models.CharField(max_length=255, blank=True)

    class Meta:
        unique_together = ("chp_area", "period", "commodity", "upload")
        ordering = ["-period", "chp_area__name", "commodity"]

    def __str__(self):
        return f"{self.chp_area} · {self.period} · {self.get_commodity_display()}"
