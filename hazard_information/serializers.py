from rest_framework import serializers

from .models import HazardInformation


class HazardInformationSerializer(serializers.ModelSerializer):
    class Meta:
        model = HazardInformation
        fields = (
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
            "min",
            "max",
            "layer_name",
            "style",
            "country",
            "model_run",
            "created_at",
        )
        read_only_fields = ("id", "created_at")
