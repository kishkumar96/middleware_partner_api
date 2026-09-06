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

    # Building/road-damage geometry dwarfs everything else in this table
    # (tens of MB per country) yet is only ever used once the Buildings/
    # Roads map overlay is switched on -- every other row (regional
    # impacts, national summary, ...) is needed immediately. Rather than
    # a separate endpoint, ?defer_large_assets=true nulls geometry out for
    # just these rows in the list response; the client re-fetches a row's
    # full geometry individually (via the retrieve view, which never
    # takes this param) only when its overlay is actually turned on.
    LARGE_ASSET_UPLOAD_MARKERS = ("damaged-building", "damaged building", "damaged-road", "damaged road")

    def to_representation(self, instance):
        data = super().to_representation(instance)
        # Read from a context flag the view opts into explicitly, not the raw
        # request query params -- otherwise a client that happens to pass
        # ?defer_large_assets=true to the retrieve-by-id endpoint would null
        # out that row's geometry too, contradicting the documented contract
        # that retrieve always returns full geometry for one row.
        if self.context.get("defer_large_assets"):
            upload_name = (instance.upload.name or "").lower()
            title = (instance.title or "").lower()
            if any(
                marker in upload_name or marker in title
                for marker in self.LARGE_ASSET_UPLOAD_MARKERS
            ):
                data["geometry"] = None
        return data


class RiskScenarioSerializer(serializers.ModelSerializer):
    # Denormalized so a consumer can filter/group by country and identify
    # which hazard product (tc/swell/combined/historical_tcs/...) a scenario
    # belongs to without a second request per risk_information id.
    country = serializers.IntegerField(source="risk_information.country_id", read_only=True)
    risk_information_title = serializers.CharField(source="risk_information.title", read_only=True)

    class Meta:
        model = RiskScenario
        fields = (
            "id",
            "risk_information",
            "risk_information_title",
            "country",
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
