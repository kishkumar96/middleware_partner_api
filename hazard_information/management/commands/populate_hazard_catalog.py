"""Bulk-populates HazardInformation rows for the Cook Islands hazard catalog
Zarr store, one row per (hazard source x SLR x return period/event)
combination, pointing at the hazard_tiles XYZ endpoint for that combination.

Covers all six hazard_tiles.views.PRODUCTS subgroups. Safe to re-run:
existing rows are matched by their natural key (country, hazard_source,
slr_scenario_m, return_period_years, event_label) and left untouched.
"""

import os

import xarray as xr
from django.conf import settings
from django.core.management.base import BaseCommand

from country.models import Country
from hazard_tiles.views import PRODUCTS
from hazard_type.models import HazardSource
from hazard_information.models import HazardInformation

STORE_PATH = os.path.join(settings.BASE_DIR, "data", "hazard_catalog.zarr")

# hazard_tiles product key -> seeded HazardSource.name
SOURCE_TO_HAZARD_SOURCE_NAME = {
    "combined": "Combined",
    "tc": "Tropical Cyclone",
    "swell": "Swell",
    "historical_tcs": "Tropical Cyclone",
    "synthetic_tcs": "Tropical Cyclone",
    "swell_driven": "Swell",
}


class Command(BaseCommand):
    help = "Populate HazardInformation rows from the hazard_catalog.zarr store."

    def add_arguments(self, parser):
        parser.add_argument(
            "--base-url",
            default="http://localhost:8000",
            help="Base URL the tile endpoint is served from (default: http://localhost:8000).",
        )

    def handle(self, *args, **options):
        base_url = options["base_url"].rstrip("/")

        country = Country.objects.filter(short_name="CK").first()
        if country is None:
            self.stderr.write("Cook Islands country row not found (expected short_name='CK').")
            return

        created, existing = 0, 0
        for source, (group, dim) in PRODUCTS.items():
            hazard_source_name = SOURCE_TO_HAZARD_SOURCE_NAME.get(source)
            hazard_source = HazardSource.objects.filter(name=hazard_source_name).first()
            if hazard_source is None:
                self.stderr.write(f"HazardSource '{hazard_source_name}' not found, skipping '{source}'.")
                continue

            ds = xr.open_zarr(STORE_PATH, group=group)
            for slr in ds["slr"].values.tolist():
                for selector in ds[dim].values.tolist():
                    url = (
                        f"{base_url}/partner_api/v1/hazard_tiles/{source}/{slr}/{selector}"
                        "/{z}/{x}/{y}.png"
                    )
                    if dim == "return_period":
                        return_period_years, event_label = selector, None
                        title = (
                            f"Cook Islands {source} flood depth — SLR {slr}m, "
                            f"{selector}yr return period"
                        )
                        layer_name = f"{source}_slr{slr}_rp{selector}"
                    else:
                        return_period_years, event_label = None, selector
                        title = f"Cook Islands {source} flood depth — SLR {slr}m, event {selector}"
                        layer_name = f"{source}_slr{slr}_event{selector}"

                    obj, was_created = HazardInformation.objects.get_or_create(
                        country=country,
                        hazard_source=hazard_source,
                        slr_scenario_m=slr,
                        return_period_years=return_period_years,
                        event_label=event_label,
                        defaults={
                            "title": title,
                            "protocol": "xyz",
                            "url": url,
                            "layer_name": layer_name,
                        },
                    )
                    created += was_created
                    existing += not was_created

        self.stdout.write(self.style.SUCCESS(f"Done. Created {created}, already existed {existing}."))
