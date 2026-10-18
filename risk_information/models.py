import os

from django.core.validators import FileExtensionValidator
from django.db import models

from event_type.models import EventType
from risk_category.models import RiskCategory

from .utils import convert_uploaded_file_to_geometry


class RiskInformation(models.Model):

    title = models.CharField(max_length=255, null=True, blank=True)
    # Stable, machine-readable role for this upload (for example
    # "forecast_floodplain" or "regional_impacts"). Clients should use this
    # instead of parsing a storage filename; blank preserves legacy uploads.
    dataset_type = models.CharField(max_length=100, blank=True, default="", db_index=True)
    risk_category = models.ForeignKey(
        RiskCategory,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="risk_information",
    )


    upload = models.FileField(
        upload_to="risk_information/",
        validators=[FileExtensionValidator(allowed_extensions=["geojson", "csv", "gpkg"])],
    )

    created_at = models.DateTimeField(auto_now_add=True)

    geometry = models.JSONField(null=True, blank=True)

    geometry_computed = models.BooleanField(default=False)

    country = models.ForeignKey(
        'country.Country',
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='risk_information',
    )

    event_type = models.ForeignKey(
        EventType,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='risk_information',
    )

    model_run = models.ForeignKey(
        "model.ModelRun",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="risk_information",
    )

    class Meta:
        db_table = "risk_information"

    def __str__(self) -> str:
        parts: list[str] = [f"RiskInformation {self.pk}"]

        if self.title:
            parts.append(self.title)

        if self.risk_category_id and self.risk_category:
            parts.append(str(self.risk_category))
        if self.country_id and self.country:
            parts.append(self.country.short_name or self.country.long_name)
        if self.event_type_id and self.event_type:
            parts.append(self.event_type.event_name)
        if self.upload:
            parts.append(os.path.basename(self.upload.name))

        return " | ".join(parts)

    def save(self, *args, **kwargs):
        is_new = self.pk is None
        old_upload_name = None
        if not is_new:
            old_upload_name = (
                RiskInformation.objects.filter(pk=self.pk)
                .values_list("upload", flat=True)
                .first()
            )

        super().save(*args, **kwargs)

        upload_changed = (
            self.upload
            and old_upload_name is not None
            and str(old_upload_name) != str(self.upload.name)
        )

        should_generate = bool(self.upload) and (self.geometry is None or is_new or upload_changed)
        if not should_generate:
            return

        try:
            with self.upload.open("rb") as file_obj:
                file_path = None
                try:
                    file_path = self.upload.path
                except Exception:
                    file_path = None
                geometry = convert_uploaded_file_to_geometry(
                    file_obj=file_obj,
                    filename=self.upload.name,
                    file_path=file_path,
                )
        except Exception:
            RiskInformation.objects.filter(pk=self.pk).update(geometry_computed=False)
            return

        def is_blank_geometry(value) -> bool:
            if value is None:
                return True
            if isinstance(value, list):
                return len(value) == 0
            if isinstance(value, dict):
                if len(value) == 0:
                    return True
                features = value.get("features")
                if isinstance(features, list) and len(features) == 0:
                    return True
            return False

        if is_blank_geometry(geometry):
            RiskInformation.objects.filter(pk=self.pk).update(
                geometry=None,
                geometry_computed=False,
            )
            return

        RiskInformation.objects.filter(pk=self.pk).update(
            geometry=geometry,
            geometry_computed=True,
        )


class RiskScenario(models.Model):
    """One national-level loss figure for a single SLR x return-period (or
    SLR x historical/synthetic event) combination, belonging to a
    RiskInformation dataset. Mirrors national-impact.csv /
    national-average-loss.csv rows. Sector- and asset-type-level detail for
    the same scenario lives in RiskScenarioBreakdown."""

    risk_information = models.ForeignKey(
        RiskInformation,
        on_delete=models.CASCADE,
        related_name="scenarios",
    )

    slr_scenario_m = models.FloatField(
        help_text="Sea level rise scenario in metres (e.g. 0, 0.25, 0.5, 1.0).",
    )

    return_period_years = models.IntegerField(
        null=True,
        blank=True,
        help_text=(
            "Return period in years for probabilistic scenarios (e.g. 10, 25, "
            "50, 100, 200, 500). Null for single historical/synthetic event "
            "rows and for annual-average-loss rows."
        ),
    )

    event_label = models.CharField(
        max_length=255,
        null=True,
        blank=True,
        help_text=(
            "Identifier for a single historical or synthetic event this row "
            "represents (e.g. a date or synthetic-catalog ID), when this row "
            "is not a return-period or annual-average-loss row."
        ),
    )

    is_annual_average_loss = models.BooleanField(
        default=False,
        help_text=(
            "True for AAL rows (national-average-loss.csv): return_period_years "
            "and event_label are null and the loss fields represent the annual "
            "average, not a single event/return-period figure."
        ),
    )

    # Exposure (Exposed.* / AAL_Exposed_* columns)
    exposed_building_value = models.FloatField(null=True, blank=True)
    exposed_road_value = models.FloatField(null=True, blank=True)
    exposed_infrastructure_value = models.FloatField(null=True, blank=True)
    exposed_crop_value = models.FloatField(null=True, blank=True)
    exposed_population = models.FloatField(null=True, blank=True)
    exposed_total_value = models.FloatField(null=True, blank=True)
    exposed_road_km = models.FloatField(null=True, blank=True)

    # Loss (Loss.* / AAL_* columns)
    total_loss = models.FloatField(null=True, blank=True)
    building_loss = models.FloatField(null=True, blank=True)
    road_loss = models.FloatField(null=True, blank=True)
    infrastructure_loss = models.FloatField(null=True, blank=True)
    crop_loss = models.FloatField(null=True, blank=True)

    class Meta:
        db_table = "risk_scenario"
        # Note: Postgres treats NULLs as distinct in unique constraints, so this
        # doesn't block duplicate AAL rows (return_period_years and event_label
        # both null) at the same SLR — acceptable for now since ingestion is
        # controlled by a single import script, not open user input.
        constraints = [
            models.UniqueConstraint(
                fields=["risk_information", "slr_scenario_m", "return_period_years", "event_label"],
                name="unique_risk_scenario_dimensions",
            ),
        ]

    def __str__(self) -> str:
        if self.is_annual_average_loss:
            scenario = "AAL"
        elif self.return_period_years:
            scenario = f"RP{self.return_period_years}"
        else:
            scenario = self.event_label or "event"
        return f"RiskScenario {self.pk} | SLR {self.slr_scenario_m} | {scenario}"


class RiskScenarioBreakdown(models.Model):
    """One sector's or asset type's share of a RiskScenario: a row of
    impact-asset-sector.csv / aal_by_sector.csv (dimension "sector") or
    impact-asset-type.csv (dimension "asset_type") for the same SLR x
    return-period/event. Losses across one scenario's rows of a dimension
    sum to the scenario's national total_loss."""

    SECTOR = "sector"
    ASSET_TYPE = "asset_type"
    DIMENSION_CHOICES = [(SECTOR, "Sector"), (ASSET_TYPE, "Asset type")]

    risk_scenario = models.ForeignKey(
        RiskScenario,
        on_delete=models.CASCADE,
        related_name="breakdowns",
    )
    dimension = models.CharField(max_length=20, choices=DIMENSION_CHOICES)
    # Sector (e.g. "Build-Commercial") or asset type (UseType, e.g. "Coconut")
    name = models.CharField(max_length=255)
    # Asset-type rows only: the sector the asset type belongs to
    sector = models.CharField(max_length=255, blank=True)

    # Asset-type rows only
    exposed_count = models.FloatField(null=True, blank=True)
    total_count = models.FloatField(null=True, blank=True)
    exposed_value = models.FloatField(null=True, blank=True)
    total_value = models.FloatField(null=True, blank=True)
    exposed_area_or_length = models.FloatField(null=True, blank=True)
    total_area_or_length = models.FloatField(null=True, blank=True)
    average_inundation_depth = models.FloatField(null=True, blank=True)
    max_inundation_depth = models.FloatField(null=True, blank=True)

    total_loss = models.FloatField(null=True, blank=True)
    building_loss = models.FloatField(null=True, blank=True)
    road_loss = models.FloatField(null=True, blank=True)
    infrastructure_loss = models.FloatField(null=True, blank=True)
    crop_loss = models.FloatField(null=True, blank=True)

    class Meta:
        db_table = "risk_scenario_breakdown"
        indexes = [models.Index(fields=["risk_scenario", "dimension"])]

    def __str__(self) -> str:
        return f"{self.get_dimension_display()} {self.name} of {self.risk_scenario}"
