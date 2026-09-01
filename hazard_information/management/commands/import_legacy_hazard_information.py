"""Imports the legacy per-country hazard_information rows (WMS pointers into
the shared THREDDS server, two per country: wind + flood) for Vanuatu,
Samoa, and Tonga from pacific-disaster-api.spc.int -- this local database
only ever had Cook Islands seeded.

Unlike risk_information, these rows carry no uploaded file -- just a WMS
protocol/url pointer plus min/max/layer_name/style -- so this is a direct
field copy, no geometry reserialization needed.

hazard_type/event_type/hazard_source are left null: the local HazardType/
EventType/HazardSource lookup tables are empty even for the existing Cook
Islands rows, so this matches that existing convention rather than
inventing a mapping.

Safe to re-run: existing HazardInformation rows are matched by
(country, title) and left untouched.
"""

import json
import urllib.error
import urllib.request

from django.core.management.base import BaseCommand

from country.models import Country
from hazard_information.models import HazardInformation

SOURCE_BASE_URL = "https://pacific-disaster-api.spc.int"
REQUEST_TIMEOUT_S = 30

# CountryCode -> (source country id on pacific-disaster-api.spc.int, local short_name, local long_name)
TARGET_COUNTRIES = {
    "VU": (2, "VU", "Vanuatu"),
    "TO": (4, "TO", "Tonga"),
    "WS": (5, "WS", "Samoa"),
}


class Command(BaseCommand):
    help = "Import Vanuatu/Samoa/Tonga hazard_information rows from pacific-disaster-api.spc.int."

    def add_arguments(self, parser):
        parser.add_argument(
            "--country",
            choices=list(TARGET_COUNTRIES.keys()) + ["all"],
            default="all",
            help="Which country to import (default: all).",
        )

    def fetch_json(self, url: str):
        with urllib.request.urlopen(url, timeout=REQUEST_TIMEOUT_S) as response:
            return json.loads(response.read().decode("utf-8"))

    def handle(self, *args, **options):
        selected = options["country"]
        codes = list(TARGET_COUNTRIES.keys()) if selected == "all" else [selected]

        url = f"{SOURCE_BASE_URL}/partner_api/v1/hazard_information/"
        try:
            all_rows = self.fetch_json(url)
        except (urllib.error.URLError, urllib.error.HTTPError, ValueError) as exc:
            self.stderr.write(f"Failed to fetch hazard_information listing: {exc}")
            return
        self.stdout.write(f"Fetched {len(all_rows)} total rows from source.")

        for code in codes:
            source_country_id, short_name, long_name = TARGET_COUNTRIES[code]
            country, _ = Country.objects.get_or_create(
                short_name=short_name,
                defaults={"long_name": long_name},
            )

            rows = [r for r in all_rows if r.get("country") == source_country_id]
            self.stdout.write(f"{code}: {len(rows)} source rows found (country id {source_country_id}).")

            created, skipped = 0, 0
            for row in rows:
                title = row.get("title") or f"{code} hazard_information {row.get('id')}"
                if HazardInformation.objects.filter(country=country, title=title).exists():
                    skipped += 1
                    continue

                HazardInformation.objects.create(
                    title=title,
                    protocol=row.get("protocol") or "wms",
                    url=row.get("url") or "",
                    min=row.get("min"),
                    max=row.get("max"),
                    layer_name=row.get("layer_name"),
                    style=row.get("style"),
                    country=country,
                )
                created += 1

            self.stdout.write(
                self.style.SUCCESS(f"{code}: created {created}, already existed {skipped}.")
            )
