"""Imports the real coastal-flood SLR/return-period hazard catalog for
Vanuatu, Tonga, and Samoa from the SPC THREDDS server
(gemthreddshpc.spc.int), one HazardInformation row per NetCDF file.

Unlike the CK dimensional catalog (data/hazard_catalog.zarr, served via the
custom hazard_tiles XYZ endpoint), this data is served directly as WMS from
THREDDS -- no Zarr conversion or tile-serving pipeline needed. Each file
covers one (island/province x return-period-or-MHWS x SLR) combination, at
paths like:

  POP/Partner2/SLR/WS/upolu/FloodDepth_upolu_100_1.0.nc
  POP/Partner2/SLR/VU/Coastal/malampa_province/FloodDepth_malampa_50_0.5.nc

slr_scenario_m / return_period_years / event_label are populated directly
from the filename so the frontend's hazard-scenario selector can filter by
them (see buildPartnerHazardLayers's WMS branch in realDataLoader.ts, which
reads these columns straight off wms-protocol rows -- unlike the CK/xyz
path, no layer_name-encoding trick is needed here).

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

# CountryCode -> (local short_name, list of THREDDS catalog subpaths under
# POP/Partner2/SLR/<subpath>/catalog.xml, one per island/province)
COUNTRY_CATALOG_PATHS = {
    "WS": ("WS", ["WS/upolu", "WS/savai'i"]),
    "VU": (
        "VU",
        [
            "VU/Coastal/malampa_province",
            "VU/Coastal/penama_province",
            "VU/Coastal/sanma_province",
            "VU/Coastal/shefa_province",
            "VU/Coastal/tafea_province",
            "VU/Coastal/torba_province",
        ],
    ),
    "TO": (
        "TO",
        [
            "TO/Coastal/eua",
            "TO/Coastal/haapai_group",
            "TO/Coastal/niuafoou",
            "TO/Coastal/niuatoputapu_tafahi_islands",
            "TO/Coastal/tongatapu",
            "TO/Coastal/vavau_group",
        ],
    ),
}

FILENAME_RE = re.compile(r"^FloodDepth_(?P<island>.+?)_(?P<rp>\d+|MHWS)_(?P<slr>[\d.]+)\.nc$")

# Matches the existing legacy WS/TO flood WMS rows' color scale (0-5m depth).
DEFAULT_MIN = 0.1
DEFAULT_MAX = 5.0
NS = {"c": "http://www.unidata.ucar.edu/namespaces/thredds/InvCatalog/v1.0"}


class Command(BaseCommand):
    help = "Import VU/TO/WS coastal-flood SLR/return-period hazard_information rows from THREDDS."

    def add_arguments(self, parser):
        parser.add_argument(
            "--country",
            choices=list(COUNTRY_CATALOG_PATHS.keys()),
            required=True,
        )

    def fetch(self, url: str) -> bytes:
        request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_S) as response:
            return response.read()

    def list_catalog_files(self, subpath: str):
        url = f"{THREDDS_BASE}/catalog/POP/Partner2/SLR/{subpath}/catalog.xml"
        try:
            xml_bytes = self.fetch(url)
        except (urllib.error.URLError, urllib.error.HTTPError) as exc:
            self.stderr.write(f"Failed to fetch catalog {url}: {exc}")
            return []

        root = ET.fromstring(xml_bytes)
        files = []
        for dataset in root.iter("{http://www.unidata.ucar.edu/namespaces/thredds/InvCatalog/v1.0}dataset"):
            name = dataset.attrib.get("name", "")
            url_path = dataset.attrib.get("urlPath")
            if not url_path or not name.endswith(".nc"):
                continue
            files.append((name, url_path))
        return files

    def handle(self, *args, **options):
        code = options["country"]
        short_name, subpaths = COUNTRY_CATALOG_PATHS[code]
        country = Country.objects.filter(short_name=short_name).first()
        if country is None:
            self.stderr.write(f"Country {short_name} not found locally; run the legacy import first.")
            return

        created, skipped, unmatched = 0, 0, 0
        for subpath in subpaths:
            files = self.list_catalog_files(subpath)
            self.stdout.write(f"{subpath}: {len(files)} files found.")

            for filename, url_path in files:
                match = FILENAME_RE.match(filename)
                if not match:
                    unmatched += 1
                    continue

                island = match.group("island")
                rp_raw = match.group("rp")
                slr = float(match.group("slr"))
                is_mhws = rp_raw == "MHWS"
                return_period_years = None if is_mhws else int(rp_raw)
                event_label = "MHWS" if is_mhws else None

                wms_url = (
                    f"{THREDDS_BASE}/wms/{url_path}?service=WMS&version=1.3.0&request=GetCapabilities"
                )
                title = (
                    f"{short_name} Coastal Flood — {island} — SLR {slr}m — "
                    f"{'MHWS' if is_mhws else f'{return_period_years}yr'}"
                )

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
                    slr_scenario_m=slr,
                    return_period_years=return_period_years,
                    event_label=event_label,
                )
                created += 1

        self.stdout.write(
            self.style.SUCCESS(
                f"{code}: created {created}, already existed {skipped}, unmatched filenames {unmatched}."
            )
        )
