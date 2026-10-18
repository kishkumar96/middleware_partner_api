from django.db import models


class Event(models.Model):
    title = models.CharField(max_length=255, blank=True, default="")
    event_datetime = models.DateTimeField(null=True, blank=True)

    event_type = models.ForeignKey(
        "event_type_v2.EventType",
        on_delete=models.PROTECT,
        related_name="events",
    )

    country = models.ForeignKey(
        "country.Country",
        on_delete=models.PROTECT,
        related_name="events",
    )

    # Only tropical-cyclone events have a track; other event types
    # (flood, earthquake, ...) have none.
    cyclone_track = models.ForeignKey(
        "cyclone_track.CycloneTrack",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="events",
    )

    hazards = models.ManyToManyField(
        "hazard_information.HazardInformation",
        blank=True,
        related_name="events",
    )

    risks = models.ManyToManyField(
        "risk_information.RiskInformation",
        blank=True,
        related_name="events",
    )

    citizen_sciences = models.ManyToManyField(
        "citizen_science.CitizenScience",
        blank=True,
        related_name="events",
    )

    model_run = models.ForeignKey(
        "model.ModelRun",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="events",
    )

    class Meta:
        db_table = "event"

    def __str__(self) -> str:
        return f"Event {self.pk}"
