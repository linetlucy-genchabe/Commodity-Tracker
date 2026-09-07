"""
All admin registrations for the Commodity Tracker. Kept in one file — see
README "Project layout".
"""

from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin

from .models import (
    County,
    Facility,
    FormDefinition,
    IndicatorSummary,
    MOH748Record,
    MOH748Upload,
    SubCounty,
    Threshold,
    User,
    Ward,
)


@admin.register(County)
class CountyAdmin(admin.ModelAdmin):
    search_fields = ["name"]


@admin.register(SubCounty)
class SubCountyAdmin(admin.ModelAdmin):
    list_display = ["name", "county"]
    list_filter = ["county"]
    search_fields = ["name"]


@admin.register(Ward)
class WardAdmin(admin.ModelAdmin):
    list_display = ["name", "sub_county"]
    list_filter = ["sub_county__county"]
    search_fields = ["name"]


@admin.register(Facility)
class FacilityAdmin(admin.ModelAdmin):
    list_display = ["name", "ward", "code"]
    list_filter = ["ward__sub_county__county"]
    search_fields = ["name", "code"]


@admin.register(User)
class UserAdmin(DjangoUserAdmin):
    fieldsets = DjangoUserAdmin.fieldsets + (
        (
            "Dashboard role & scope",
            {"fields": ("role", "county", "sub_county", "facility", "can_upload")},
        ),
    )
    list_display = ["username", "email", "role", "county", "sub_county", "facility", "can_upload", "is_staff"]
    list_filter = ["role", "is_staff", "is_superuser"]


@admin.register(FormDefinition)
class FormDefinitionAdmin(admin.ModelAdmin):
    list_display = ["short_name", "name", "category", "is_active", "display_order"]
    list_editable = ["is_active", "display_order"]


@admin.register(Threshold)
class ThresholdAdmin(admin.ModelAdmin):
    list_display = ["form", "metric_key", "label", "green_max", "amber_max", "is_draft"]
    list_filter = ["form", "is_draft"]


@admin.register(IndicatorSummary)
class IndicatorSummaryAdmin(admin.ModelAdmin):
    list_display = ["facility", "period", "indicator_label", "value", "status"]
    list_filter = ["form", "status", "period"]
    search_fields = ["facility__name"]

    def has_add_permission(self, request):
        return False

    def get_readonly_fields(self, request, obj=None):
        return [f.name for f in self.model._meta.fields]

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(MOH748Upload)
class MOH748UploadAdmin(admin.ModelAdmin):
    list_display = ["period", "source_filename", "uploaded_by", "uploaded_at", "facility_rows_parsed", "record_rows_created"]
    list_filter = ["period"]


@admin.register(MOH748Record)
class MOH748RecordAdmin(admin.ModelAdmin):
    list_display = ["facility", "period", "commodity", "days_out_of_stock", "physical_count"]
    list_filter = ["period", "commodity"]
    search_fields = ["facility__name"]
