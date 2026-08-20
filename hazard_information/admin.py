from django.contrib import admin

from .models import HazardInformation


@admin.register(HazardInformation)
class HazardInformationAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "title",
        "protocol",
        "url",
        "hazard_type",
        "event_type",
        "hazard_source",
        "slr_scenario_m",
        "return_period_years",
        "event_label",
        "country",
        "model_run",
        "layer_name",
        "min",
        "max",
        "created_at",
    )
    list_filter = ("hazard_source", "slr_scenario_m", "return_period_years")
    list_select_related = (
        "country",
        "hazard_type",
        "event_type",
        "hazard_source",
        "model_run",
    )
    search_fields = (
        "title",
        "protocol",
        "url",
        "layer_name",
        "event_label",
        "hazard_type__hazard_type",
        "event_type__event_name",
        "hazard_source__name",
        "country__short_name",
        "country__long_name",
        "model_run__name",
    )
