from django.db import models


class HazardType(models.Model):
    hazard_type = models.CharField(max_length=255)

    def __str__(self) -> str:
        return self.hazard_type


class HazardSource(models.Model):
    """The forcing mechanism that produced a hazard layer (e.g. tropical
    cyclone, swell, or a combination of both) — orthogonal to HazardType,
    which describes the physical phenomenon (wind, inundation, etc.)."""

    name = models.CharField(max_length=255, unique=True)

    class Meta:
        db_table = "hazard_source"

    def __str__(self) -> str:
        return self.name
