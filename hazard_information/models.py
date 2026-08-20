from django.db import models

from country.models import Country
from event_type.models import EventType
from hazard_type.models import HazardSource, HazardType


class HazardInformation(models.Model):

    title = models.CharField(max_length=255, null=True, blank=True)
    protocol = models.CharField(max_length=50)
    url = models.URLField(max_length=2000)


    created_at = models.DateTimeField(auto_now_add=True)

    hazard_type = models.ForeignKey(
        HazardType,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="hazard_information",
    )

    event_type = models.ForeignKey(
        EventType,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="hazard_information",
    )

    min = models.FloatField(null=True, blank=True)
    max = models.FloatField(null=True, blank=True)

    layer_name = models.CharField(max_length=255, null=True, blank=True)
    style = models.CharField(max_length=255, null=True, blank=True)

    country = models.ForeignKey(
        Country,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="hazard_information",
    )

    model_run = models.ForeignKey(
        "model.ModelRun",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="hazard_information",
    )

    # Scenario dimensions for probabilistic / multi-scenario hazard catalogs
    # (e.g. Cook Islands: hazard source x SLR scenario x return period, or
    # x a specific historical/synthetic event). One row still equals one
    # published WMS layer/URL; these fields let that layer be filtered and
    # grouped by scenario instead of being an opaque, undated upload.
    hazard_source = models.ForeignKey(
        HazardSource,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="hazard_information",
    )

    slr_scenario_m = models.FloatField(
        null=True,
        blank=True,
        help_text="Sea level rise scenario in metres (e.g. 0, 0.25, 0.5, 1.0).",
    )

    return_period_years = models.IntegerField(
        null=True,
        blank=True,
        help_text=(
            "Return period in years for probabilistic layers (e.g. 10, 25, 50, "
            "100, 200, 500). Null for single historical/synthetic event layers."
        ),
    )

    event_label = models.CharField(
        max_length=255,
        null=True,
        blank=True,
        help_text=(
            "Identifier for a single historical or synthetic event this layer "
            "represents (e.g. a date or synthetic-catalog ID), when this row "
            "is not a return-period/probabilistic layer."
        ),
    )

    class Meta:
        db_table = "hazard_information"

    def __str__(self) -> str:
        parts: list[str] = []
        if self.title:
            parts.append(self.title)
        if self.country_id and self.country:
            parts.append(self.country.short_name or self.country.long_name)
        if self.event_type_id and self.event_type:
            parts.append(self.event_type.event_name)
        if self.layer_name:
            parts.append(self.layer_name)
        parts.append(self.protocol)
        prefix = " | ".join(parts)
        return f"{prefix}: {self.url}"
