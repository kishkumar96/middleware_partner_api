"""Imports a real Event for Vanuatu's Tropical Cyclone Lola (2023-10-23),
the pilot for wiring the Event/CycloneTrack tables that PARTneR2's frontend
has always been able to read but that were never populated.

Source of the track data: `data/vu_lola_forecast.csv`, a genuine RSMC-style
best-track/forecast CSV (13 six-hourly observations, 2023-10-23T03:00Z to
2023-10-26T03:00Z: lat/lon/pressure/wind/category/etc.) that was already
shipped in this project's own frontend repo (Partner2) as
`public/vanuatu/cyclone-lola-forecast.csv`, until commit 4c2fc3e
("wire partner API integration and remove static country data") deleted it
on the assumption the same data would be available dynamically from this
API. It never was -- CycloneTrack/Event were never actually populated, so
every country's "Active event context" has been showing a generic
"Tropical Cyclone" fallback ever since. Recovered here from that commit's
frontend git history rather than fabricated; not sourced from RSMC/JTWC/
IBTrACS directly, since no direct access to those was available in this
session -- treat `cyclone_track.geometry`'s issued_agency as unconfirmed
until someone can verify it against an authoritative archive.

The two HazardInformation rows linked below (ids 9409/9410, "TC Lola Flood
Hazard"/"TC Lola Wind Hazard") were matched by title, not guessed: they are
the only VU rows whose title or THREDDS URL mentions "Lola" (both point to
real, live gemthreddshpc.spc.int datasets under vu_hazard/TC/Lola/). No
RiskInformation row mentions Lola by name, so risks is left empty rather
than guessed at.

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

TRACK_CSV_PATH = Path(__file__).resolve().parent / "data" / "vu_lola_forecast.csv"
EVENT_TITLE = "Tropical Cyclone Lola"
EVENT_DATETIME = datetime.datetime(2023, 10, 23, 3, 0, 0, tzinfo=datetime.timezone.utc)
LOLA_HAZARD_INFORMATION_IDS = [9409, 9410]


class Command(BaseCommand):
    help = "Import the Tropical Cyclone Lola Event + CycloneTrack for Vanuatu."

    def handle(self, *args, **options):
        vu = Country.objects.get(short_name="VU")

        if Event.objects.filter(country=vu, title=EVENT_TITLE).exists():
            self.stdout.write(self.style.WARNING(f"{EVENT_TITLE} event already exists, skipping."))
            return

        event_type, _ = EventType.objects.get_or_create(event_name="Tropical Cyclone")

        track = CycloneTrack.objects.filter(cyclone_name="Lola", country=vu).first()
        if track is None:
            with open(TRACK_CSV_PATH, "rb") as f:
                csv_bytes = f.read()
            track = CycloneTrack(cyclone_name="Lola", country=vu)
            track.track_file.save("cyclone-lola-forecast.csv", ContentFile(csv_bytes), save=True)
            track.refresh_from_db()

        if not track.geometry_computed:
            self.stdout.write(
                self.style.ERROR(
                    f"CycloneTrack {track.id}'s track CSV failed to parse into geometry -- "
                    "check cyclone_track.utils.cyclone_csv_to_geojson against the source file."
                )
            )
            return

        hazards = HazardInformation.objects.filter(id__in=LOLA_HAZARD_INFORMATION_IDS, country=vu)
        if hazards.count() != len(LOLA_HAZARD_INFORMATION_IDS):
            self.stdout.write(
                self.style.WARNING(
                    f"Expected {len(LOLA_HAZARD_INFORMATION_IDS)} matching HazardInformation rows, "
                    f"found {hazards.count()} -- hazard ids may have changed since this command was "
                    "written. Proceeding with whatever matched."
                )
            )

        event = Event.objects.create(
            title=EVENT_TITLE,
            event_datetime=EVENT_DATETIME,
            event_type=event_type,
            country=vu,
            cyclone_track=track,
        )
        event.hazards.set(hazards)

        self.stdout.write(
            self.style.SUCCESS(
                f"Created Event {event.id} ({EVENT_TITLE}), CycloneTrack {track.id} "
                f"({len(track.geometry['features'])} track points), linked to "
                f"{hazards.count()} hazard row(s)."
            )
        )
