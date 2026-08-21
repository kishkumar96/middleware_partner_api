"""Imports Cook Islands risk_information rows from the live opmthredds.gem.spc.int
backend, a separate Django deployment that already has real, correctly-tagged
CK risk data (country_id=4 there) that pacific-disaster-api.spc.int does not.

The source server's own media/file storage for these uploads returns
502/404 (confirmed dead), but the API's detail endpoint still returns each
row's fully parsed `geometry` inline. So instead of downloading the original
upload, this re-serializes that already-parsed geometry back into a file
matching its original extension (.geojson -> json.dumps, .csv ->
csv.DictWriter) and saves *that* as RiskInformation.upload. The model's own
save() hook then reparses it via convert_uploaded_file_to_geometry, which
both regenerates `geometry` locally and validates the round-trip.

Safe to re-run: existing rows are matched by (country, title) and left
untouched.
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

SOURCE_BASE_URL = "https://opmthredds.gem.spc.int"
SOURCE_COUNTRY_ID = 4  # Cook Islands ("COK") on the opmthredds backend
# The source API's `?country=` filter is unreliable (observed to ignore the
# param entirely), so rows are fetched by known id and the returned
# `country` field is checked directly instead of trusting the query filter.
SOURCE_RISK_INFORMATION_IDS = [28, 29, 30, 31, 32, 33, 34]

REQUEST_TIMEOUT_S = 30


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
    help = "Import Cook Islands risk_information rows from opmthredds.gem.spc.int."

    def fetch_json(self, url: str):
        with urllib.request.urlopen(url, timeout=REQUEST_TIMEOUT_S) as response:
            return json.loads(response.read().decode("utf-8"))

    def handle(self, *args, **options):
        country = Country.objects.filter(short_name="CK").first()
        if country is None:
            self.stderr.write("Cook Islands country row not found (expected short_name='CK').")
            return

        created, skipped, failed = 0, 0, 0

        for row_id in SOURCE_RISK_INFORMATION_IDS:
            detail_url = f"{SOURCE_BASE_URL}/partner_api/v1/risk_information/{row_id}"
            try:
                row = self.fetch_json(detail_url)
            except (urllib.error.URLError, urllib.error.HTTPError, ValueError) as exc:
                self.stderr.write(f"Failed to fetch {detail_url}: {exc}")
                failed += 1
                continue

            if row.get("country") != SOURCE_COUNTRY_ID:
                self.stderr.write(
                    f"Row {row_id} has country={row.get('country')!r}, expected "
                    f"{SOURCE_COUNTRY_ID}; skipping (source API's country filter is "
                    "unreliable, so this is checked per-row)."
                )
                failed += 1
                continue

            title = row.get("title") or f"CK risk_information {row_id}"
            if RiskInformation.objects.filter(country=country, title=title).exists():
                skipped += 1
                continue

            upload_url = row.get("upload")
            geometry = row.get("geometry")
            if not upload_url or geometry is None:
                self.stderr.write(
                    f"Row {row_id} ({title}) has no upload URL or geometry; skipping."
                )
                failed += 1
                continue

            filename = upload_url.rsplit("/", 1)[-1]
            try:
                file_bytes = reserialize_geometry(geometry, filename)
            except ValueError as exc:
                self.stderr.write(f"Row {row_id} ({title}): {exc}")
                failed += 1
                continue

            obj = RiskInformation(country=country, title=title)
            # .upload.save() writes the file and calls obj.save(), which is
            # what triggers the model's own geometry-computation logic.
            obj.upload.save(filename, ContentFile(file_bytes), save=True)

            obj.refresh_from_db()
            if not obj.geometry_computed:
                self.stderr.write(
                    f"Row {row_id} ({title}) saved but geometry_computed=False — "
                    "check the file parsed correctly."
                )

            created += 1

        self.stdout.write(
            self.style.SUCCESS(
                f"Done. Created {created}, already existed {skipped}, failed {failed}."
            )
        )
