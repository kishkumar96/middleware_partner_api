from django.contrib import admin

from .models import RiskInformation, RiskScenario


@admin.register(RiskInformation)
class RiskInformationAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "title",
        "risk_category",
        "upload",
        "geometry_computed",
        "created_at",
        "country",
        "event_type",
        "model_run",
    )
    list_select_related = ("risk_category", "country", "event_type", "model_run")
    search_fields = (
        "title",
        "risk_category__short_name",
        "risk_category__long_name",
        "event_type__event_name",
        "country__short_name",
        "country__long_name",
        "model_run__name",
        "upload",
    )


@admin.register(RiskScenario)
class RiskScenarioAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "risk_information",
        "slr_scenario_m",
        "return_period_years",
        "event_label",
        "is_annual_average_loss",
        "total_loss",
    )
    list_filter = ("slr_scenario_m", "return_period_years", "is_annual_average_loss")
    list_select_related = ("risk_information",)
    search_fields = ("event_label", "risk_information__title")

