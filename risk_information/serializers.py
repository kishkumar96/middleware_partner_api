from rest_framework import serializers

from .models import RiskInformation, RiskScenario


class RiskInformationSerializer(serializers.ModelSerializer):
    class Meta:
        model = RiskInformation
        fields = (
            "id",
            "title",
            "risk_category",
            "upload",
            "geometry",
            "geometry_computed",
            "created_at",
            "country",
            "event_type",
            "model_run",
        )
        read_only_fields = (
            "id",
            "geometry",
            "geometry_computed",
            "created_at",
        )


class RiskScenarioSerializer(serializers.ModelSerializer):
    class Meta:
        model = RiskScenario
        fields = (
            "id",
            "risk_information",
            "slr_scenario_m",
            "return_period_years",
            "event_label",
            "is_annual_average_loss",
            "exposed_building_value",
            "exposed_road_value",
            "exposed_infrastructure_value",
            "exposed_crop_value",
            "exposed_population",
            "exposed_total_value",
            "exposed_road_km",
            "total_loss",
            "building_loss",
            "road_loss",
            "infrastructure_loss",
            "crop_loss",
        )
        read_only_fields = ("id",)
