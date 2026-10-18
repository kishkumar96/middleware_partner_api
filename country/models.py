from django.core.validators import RegexValidator
from django.db import models

from .codes import CODE_PATTERN, normalise_code

class Country(models.Model):
    # The same in every deployment, unlike the id and the names (see country/codes.py). Optional so
    # existing rows stay valid until set; unique, and blank is stored as NULL so several can be unset.
    code = models.CharField(
        max_length=2,
        unique=True,
        null=True,
        blank=True,
        validators=[RegexValidator(CODE_PATTERN, "Use a two-letter ISO country code in capitals, e.g. CK.")],
        help_text="ISO 3166-1 alpha-2 code, e.g. CK. Clients should identify a country by this, not by id or name.",
    )
    short_name = models.CharField(max_length=255)
    long_name = models.CharField(max_length=255)
    west_bound_longitude = models.FloatField(null=True, blank=True)
    east_bound_longitude = models.FloatField(null=True, blank=True)
    south_bound_latitude = models.FloatField(null=True, blank=True)
    north_bound_latitude = models.FloatField(null=True, blank=True)
    crs = models.CharField(max_length=255, null=True, blank=True)

    regions = models.ManyToManyField(
        'region.Region',
        blank=True,
        related_name='countries',
    )

    def save(self, *args, **kwargs):
        self.code = normalise_code(self.code)
        super().save(*args, **kwargs)

    def __str__(self):
        return self.short_name
