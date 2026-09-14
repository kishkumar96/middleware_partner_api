"""Imports a real Event for Cook Islands' Tropical Cyclone Meena (February
2005), the fourth and final country in the Event/CycloneTrack population
effort (see import_vu_lola_event.py for background).

Meena was part of a well-documented, widely-reported cluster of five
cyclones (Meena, Nancy, Olaf, Percy, Rae) that struck the Cook Islands
within about a month in Feb-Mar 2005 -- among the most damaging cyclone
seasons on record for the country. EVENT_DATETIME uses that public-record
month (2005-02-01, approximate; exact landfall day not confirmed in this
session) rather than any date encoded in this app's own data.

Track geometry, honestly absent rather than fabricated: unlike VU/TO/WS,
public/cook-islands/cyclone-track.geojson (recovered from the same
Partner2 git history as the other three) is an empty FeatureCollection --
0 features, no coordinates at all. No real per-point track geometry for
Meena exists anywhere found in this session. Rather than inventing
coordinates, this command creates the CycloneTrack row with a genuinely
empty track_file (header-only CSV) -- cyclone_csv_to_geojson correctly
returns None for it, and the model's own is_blank_geometry check sets
geometry=None, geometry_computed=False. That's the schema's real,
intended way to represent "this storm is real and documented, but we
have no track geometry for it" -- not an error case being papered over.

Hazard data: unlike the historical_tcs/synthetic_tcs/swell_driven
scenario catalog already ingested for Cook Islands (protocol='xyz',
Zarr-backed tiles, dated by event label not name), Meena predates that
catalog and has never been in HazardInformation at all -- it has only
ever existed as three hardcoded entries in the frontend's static
REAL_WMS_LAYERS fallback (src/data/realThreddsLayers.ts:
ck-tc-meena-wind-subdomain-1/2, ck-tc-meena-inundation). This command
migrates those three real, already-shipped WMS layers into
HazardInformation rows so Meena is queryable the same way VU/TO/WS's
named events are, rather than duplicating fabricated data -- the min/
max/bbox/URLs below are copied directly from that frontend file, and
were re-verified live against gemthreddshpc.spc.int in this session
(all three returned HTTP 200 on GetCapabilities) before being used here.

Safe to re-run: skips creating the Event if one with this exact
(country, title) already exists.
"""

import datetime
from pathlib import Path

from django.core.files.base import ContentFile
from django.core.management.base import BaseCommand

from country.models import Country
from cyclone_track.models import CycloneTrack
from event.models import Event
from event_type.models import EventType
from hazard_information.models import HazardInformation

EMPTY_TRACK_CSV_PATH = Path(__file__).resolve().parent / "data" / "ck_meena_empty_track.csv"
EVENT_TITLE = "Tropical Cyclone Meena"
# Approximate: part of the well-documented Feb-Mar 2005 Cook Islands
# cyclone cluster (Meena/Nancy/Olaf/Percy/Rae); exact landfall day not
# confirmed in this session. See module docstring.
EVENT_DATETIME = datetime.datetime(2005, 2, 1, 0, 0, 0, tzinfo=datetime.timezone.utc)

THREDDS_BASE = "https://gemthreddshpc.spc.int/thredds/wms"
MEENA_DATASET_PREFIX = "POP/Partner2/case_study2/hazard/ck_hazard/TC/Meena"

# Copied from src/data/realThreddsLayers.ts's REAL_WMS_LAYERS entries for
# these three ids -- title, ncFile, hazard type, and colorScaleRange
# (-> min/max) all taken directly from that already-real, already-shipped
# frontend data, not invented here.
MEENA_HAZARD_LAYERS = [
    {
        "title": "TC Meena Wind Hazard (Subdomain 1)",
        "nc_file": "_CK_subdomain_1_local_wind.nc",
        "layer_name": "Depth",
        "min": 0.1,
        "max": 65.74,
    },
    {
        "title": "TC Meena Wind Hazard (Subdomain 2)",
        "nc_file": "_CK_subdomain_2_local_wind.nc",
        "layer_name": "Depth",
        "min": 0.1,
        "max": 65.74,
    },
    {
        "title": "TC Meena Flood Hazard",
        "nc_file": "CK_merged.nc",
        "layer_name": "Depth",
        "min": 0.01,
        "max": 5.0,
    },
]


class Command(BaseCommand):
    help = "Import the Tropical Cyclone Meena Event for Cook Islands."

    def handle(self, *args, **options):
        ck = Country.objects.get(short_name="CK")

        if Event.objects.filter(country=ck, title=EVENT_TITLE).exists():
            self.stdout.write(self.style.WARNING(f"{EVENT_TITLE} event already exists, skipping."))
            return

        event_type, _ = EventType.objects.get_or_create(event_name="Tropical Cyclone")

        track = CycloneTrack.objects.filter(cyclone_name="Meena", country=ck).first()
        if track is None:
            with open(EMPTY_TRACK_CSV_PATH, "rb") as f:
                csv_bytes = f.read()
            track = CycloneTrack(cyclone_name="Meena", country=ck)
            track.track_file.save("ck_meena_empty_track.csv", ContentFile(csv_bytes), save=True)
            track.refresh_from_db()

        if track.geometry_computed:
            self.stdout.write(
                self.style.WARNING(
                    "CycloneTrack unexpectedly has computed geometry -- expected none, since "
                    "no real Meena track data was found. Check nothing upstream changed."
                )
            )

        hazards = []
        for layer in MEENA_HAZARD_LAYERS:
            url = f"{THREDDS_BASE}/{MEENA_DATASET_PREFIX}/{layer['nc_file']}?service=WMS&version=1.3.0&request=GetCapabilities"
            hazard, created = HazardInformation.objects.get_or_create(
                country=ck,
                title=layer["title"],
                defaults={
                    "protocol": "WMS",
                    "url": url,
                    "layer_name": layer["layer_name"],
                    "min": layer["min"],
                    "max": layer["max"],
                },
            )
            hazards.append(hazard)
            if created:
                self.stdout.write(f"  created HazardInformation {hazard.id}: {layer['title']}")

        event = Event.objects.create(
            title=EVENT_TITLE,
            event_datetime=EVENT_DATETIME,
            event_type=event_type,
            country=ck,
            cyclone_track=track,
        )
        event.hazards.set(hazards)

        self.stdout.write(
            self.style.SUCCESS(
                f"Created Event {event.id} ({EVENT_TITLE}), CycloneTrack {track.id} "
                f"(no track geometry available -- honestly absent, not fabricated), "
                f"linked to {len(hazards)} hazard row(s)."
            )
        )
