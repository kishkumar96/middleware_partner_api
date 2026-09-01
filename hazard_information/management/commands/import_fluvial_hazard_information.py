"""Imports Vanuatu's fluvial/pluvial (rainfall & river) flood hazard catalog
from THREDDS -- a second, distinct hazard product alongside the coastal
SLR flood catalog imported by import_slr_hazard_information.py.

Only Vanuatu has this dataset; Tonga and Samoa's THREDDS trees
(case_study2/hazard/to_hazard, ws_hazard) contain only the single legacy
TC event, no Floodplains/Fluvial folder -- confirmed by browsing both
directly. That's a genuine data gap, not something this command can fix
for those two.

Source layout: POP/Partner2/case_study2/hazard/vu_hazard/Fluvial/
  Pluvial-Fluvial_{Location}[_12hr]_ARI{n}_hmax.nc, one file per
  (location x return period) combination, across Efate, Santo, and Tanna.
  Malekula only has .tiff versions (no .nc) and is skipped -- ncWMS access
  for those wasn't verified.

Unlike the coastal SLR product, this hazard has no SLR dimension, so
slr_scenario_m is stored as a fixed 0 sentinel for every row (matching the
buildPartnerHazardLayers frontend, which requires a non-null slr_scenario_m
to treat a WMS row as scenario-tagged at all) -- the actual dimension a
user picks between is return_period_years.

Safe to re-run: existing rows are matched by (country, url) and left
untouched.
"""

import re
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET

from django.core.management.base import BaseCommand

from country.models import Country
from hazard_information.models import HazardInformation

THREDDS_BASE = "https://gemthreddshpc.spc.int/thredds"
REQUEST_TIMEOUT_S = 30
CATALOG_SUBPATH = "case_study2/hazard/vu_hazard/Fluvial"

FILENAME_RE = re.compile(
    r"^Pluvial-Fluvial_(?P<location>[A-Za-z]+)(?:_12hr)?_ARI(?P<ari>\d+)_hmax\.nc$"
)

# Matches the coastal-flood import's color scale (0-5m depth).
DEFAULT_MIN = 0.1
DEFAULT_MAX = 5.0
NS = {"c": "http://www.unidata.ucar.edu/namespaces/thredds/InvCatalog/v1.0"}


class Command(BaseCommand):
    help = "Import Vanuatu's fluvial/pluvial flood hazard_information rows from THREDDS."

    def fetch(self, url: str) -> bytes:
        request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_S) as response:
            return response.read()

    def handle(self, *args, **options):
        country = Country.objects.filter(short_name="VU").first()
        if country is None:
            self.stderr.write("Vanuatu country row not found; run the legacy import first.")
            return

        catalog_url = f"{THREDDS_BASE}/catalog/POP/Partner2/{CATALOG_SUBPATH}/catalog.xml"
        try:
            xml_bytes = self.fetch(catalog_url)
        except (urllib.error.URLError, urllib.error.HTTPError) as exc:
            self.stderr.write(f"Failed to fetch catalog {catalog_url}: {exc}")
            return

        root = ET.fromstring(xml_bytes)
        files = []
        for dataset in root.iter(
            "{http://www.unidata.ucar.edu/namespaces/thredds/InvCatalog/v1.0}dataset"
        ):
            name = dataset.attrib.get("name", "")
            url_path = dataset.attrib.get("urlPath")
            if not url_path or not name.endswith(".nc"):
                continue
            files.append((name, url_path))

        self.stdout.write(f"Fetched {len(files)} .nc files from {CATALOG_SUBPATH}.")

        created, skipped, unmatched = 0, 0, 0
        for filename, url_path in files:
            match = FILENAME_RE.match(filename)
            if not match:
                unmatched += 1
                self.stderr.write(f"Filename didn't match expected pattern: {filename}")
                continue

            location = match.group("location")
            return_period_years = int(match.group("ari"))

            wms_url = f"{THREDDS_BASE}/wms/{url_path}?service=WMS&version=1.3.0&request=GetCapabilities"
            title = f"VU Fluvial Flood — {location} — {return_period_years}yr ARI"

            if HazardInformation.objects.filter(country=country, url=wms_url).exists():
                skipped += 1
                continue

            HazardInformation.objects.create(
                title=title,
                protocol="wms",
                url=wms_url,
                min=DEFAULT_MIN,
                max=DEFAULT_MAX,
                layer_name="Depth",
                style="default-scalar/x-Sst",
                country=country,
                slr_scenario_m=0,
                return_period_years=return_period_years,
                event_label=None,
            )
            created += 1

        self.stdout.write(
            self.style.SUCCESS(
                f"VU Fluvial: created {created}, already existed {skipped}, unmatched {unmatched}."
            )
        )
