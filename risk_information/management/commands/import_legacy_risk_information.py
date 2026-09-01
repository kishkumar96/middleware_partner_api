"""Imports the legacy, per-country risk_information rows (damaged-buildings,
damaged-roads, exposure-by-cluster, impact-by-asset-type, impact-by-sector,
national-summary, regional-impacts, regional-impacts-by-sector,
regional-summary, regional-summary-by-sector) for Vanuatu, Samoa, and Tonga
from pacific-disaster-api.spc.int, the deployment that already has these
ten rows per country -- this local database only ever had Cook Islands
seeded (via import_ck_risk_information.py / import_ck_risk_scenarios.py).

Mirrors import_ck_risk_information.py's approach: the source's own file
storage isn't guaranteed reachable, so this re-serializes each row's
already-parsed `geometry` back into a file matching its original extension
and saves that as RiskInformation.upload; the model's own save() hook
reparses it via convert_uploaded_file_to_geometry, regenerating `geometry`
locally and validating the round-trip.

Unlike import_ck_risk_information.py, the source row IDs aren't hardcoded --
the source API's `?country=` filter is unreliable (observed to ignore the
param entirely), so this fetches the full paginated risk_information
listing and filters by each row's own `country` field, discovering
whichever IDs currently belong to each target country instead of assuming
a fixed set.

Country rows for VU/WS/TO are created here too (get_or_create by
short_name) since this database has never had them.

Safe to re-run: existing RiskInformation rows are matched by
(country, title) and left untouched.
"""

import csv
import io
import json
import urllib.error
import urllib.request

from django.core.files.base import ContentFile
from django.core.management.base import BaseCommand

from country.models import Country
from risk_information.models import RiskInformation

SOURCE_BASE_URL = "https://pacific-disaster-api.spc.int"
REQUEST_TIMEOUT_S = 60

# CountryCode -> (source country id on pacific-disaster-api.spc.int, local short_name, local long_name)
TARGET_COUNTRIES = {
    "VU": (2, "VU", "Vanuatu"),
    "TO": (4, "TO", "Tonga"),
    "WS": (5, "WS", "Samoa"),
}


def reserialize_geometry(geometry, filename: str) -> bytes:
    """Turns an already-parsed geometry value back into file bytes matching
    the format convert_uploaded_file_to_geometry expects for this extension,
    so the model's own save() hook can reparse it losslessly."""
    ext = filename.rsplit(".", 1)[-1].lower()

    if ext == "geojson":
        return json.dumps(geometry).encode("utf-8")

    if ext == "csv":
        rows = geometry if isinstance(geometry, list) else []
        buffer = io.StringIO()
        if rows:
            writer = csv.DictWriter(buffer, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)
        return buffer.getvalue().encode("utf-8")

    raise ValueError(f"Don't know how to reserialize geometry for extension '.{ext}'")


class Command(BaseCommand):
    help = "Import Vanuatu/Samoa/Tonga risk_information rows from pacific-disaster-api.spc.int."

    def add_arguments(self, parser):
        parser.add_argument(
            "--country",
            choices=list(TARGET_COUNTRIES.keys()) + ["all"],
            default="all",
            help="Which country to import (default: all).",
        )

    def fetch_json(self, url: str):
        # The source is behind Cloudflare, which blocks urllib's default
        # "Python-urllib/x.y" user agent.
        request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_S) as response:
            return json.loads(response.read().decode("utf-8"))

    def fetch_all_risk_information(self):
        """The source's `?country=` filter is unreliable, so this fetches
        every page once (unfiltered) and callers filter client-side by each
        row's own `country` field."""
        url = f"{SOURCE_BASE_URL}/partner_api/v1/risk_information/"
        rows = []
        safety_counter = 0
        while url and safety_counter < 20:
            safety_counter += 1
            payload = self.fetch_json(url)
            rows.extend(payload.get("results", []))
            url = payload.get("next")
        return rows

    def handle(self, *args, **options):
        selected = options["country"]
        codes = list(TARGET_COUNTRIES.keys()) if selected == "all" else [selected]

        self.stdout.write("Fetching full risk_information listing from source...")
        try:
            all_rows = self.fetch_all_risk_information()
        except (urllib.error.URLError, urllib.error.HTTPError, ValueError) as exc:
            self.stderr.write(f"Failed to fetch risk_information listing: {exc}")
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

            created, skipped, failed = 0, 0, 0
            for row in rows:
                title = row.get("title") or f"{code} risk_information {row.get('id')}"
                if RiskInformation.objects.filter(country=country, title=title).exists():
                    skipped += 1
                    continue

                upload_url = row.get("upload")
                geometry = row.get("geometry")
                if not upload_url or geometry is None:
                    self.stderr.write(
                        f"{code} row {row.get('id')} ({title}) has no upload URL or geometry; skipping."
                    )
                    failed += 1
                    continue

                filename = upload_url.rsplit("/", 1)[-1]
                try:
                    file_bytes = reserialize_geometry(geometry, filename)
                except ValueError as exc:
                    self.stderr.write(f"{code} row {row.get('id')} ({title}): {exc}")
                    failed += 1
                    continue

                obj = RiskInformation(country=country, title=title)
                obj.upload.save(filename, ContentFile(file_bytes), save=True)

                obj.refresh_from_db()
                if not obj.geometry_computed:
                    self.stderr.write(
                        f"{code} row {row.get('id')} ({title}) saved but geometry_computed=False — "
                        "check the file parsed correctly."
                    )

                created += 1

            self.stdout.write(
                self.style.SUCCESS(
                    f"{code}: created {created}, already existed {skipped}, failed {failed}."
                )
            )
