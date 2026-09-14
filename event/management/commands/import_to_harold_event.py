"""Imports a real Event for Tonga's Tropical Cyclone Harold (April 2020),
the second country in the Event/CycloneTrack population effort started with
Vanuatu's TC Lola (see import_vu_lola_event.py for the full background on
why these tables were empty).

Track data provenance -- read this before treating the data as more precise
than it is: `data/to_harold_track.csv` is derived from
public/Tonga/cyclone-track.geojson, a real file that existed in the
frontend repo (Partner2) before being deleted in commit 4c2fc3e, the same
partner-API migration that deleted Vanuatu's Lola forecast. Its 55
coordinates are real and geographically consistent with Harold's actual
public-record path (Solomon Islands -> Vanuatu -> weakening past Fiji ->
near Tonga, matching the widely-reported April 2020 timeline) -- unlike
Vanuatu's source file, this one is a bare LineString with a
{"event": "Tropical Cyclone Harold"} property and NO per-point timestamps
at all. Rather than inventing timestamps that were never in the source
data, this command's CSV leaves every row's Time cell blank -- each
feature's geometry is real, its per-point time is honestly absent, not
fabricated. EVENT_DATETIME below is deliberately a single approximate date
(Harold's well-documented Vanuatu landfall, 2020-04-06) for the Event
record as a whole, sourced from public reporting on the storm rather than
this dataset -- it should not be read as this specific track's precise
timing.

The two linked HazardInformation rows (9411/9412, "TC Harold Flood Hazard"/
"TC harold wind hazard" [sic]) were matched by title, not assumed: they are
the only TO rows whose title or THREDDS URL mentions "harold", both
pointing to real, live gemthreddshpc.spc.int datasets under Harold_TO/.
No RiskInformation row mentions Harold by name, so risks is left empty.

A third file recovered from the same frontend history,
public/samoa/cyclone-track-gita.geojson, is deliberately NOT imported by
a similar command. Unlike this file, it carries zero properties (no name,
no event label) and spans an implausibly vast area (10 deg S to 49 deg S) --
Gita's remnants did become an intense extratropical low south of NZ in
real life, so this isn't impossible, but there's no way to confirm from
the file alone that it's genuinely Gita and not an unrelated placeholder.
Left for a human with access to an authoritative track archive (IBTrACS/
RSMC Nadi) to confirm before it's imported.

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

TRACK_CSV_PATH = Path(__file__).resolve().parent / "data" / "to_harold_track.csv"
EVENT_TITLE = "Tropical Cyclone Harold"
# Approximate: Harold's documented Vanuatu landfall date, not derived from
# this track file (which has no per-point timestamps). See module docstring.
EVENT_DATETIME = datetime.datetime(2020, 4, 6, 0, 0, 0, tzinfo=datetime.timezone.utc)
HAROLD_HAZARD_INFORMATION_IDS = [9411, 9412]


class Command(BaseCommand):
    help = "Import the Tropical Cyclone Harold Event + CycloneTrack for Tonga."

    def handle(self, *args, **options):
        to = Country.objects.get(short_name="TO")

        if Event.objects.filter(country=to, title=EVENT_TITLE).exists():
            self.stdout.write(self.style.WARNING(f"{EVENT_TITLE} event already exists, skipping."))
            return

        event_type, _ = EventType.objects.get_or_create(event_name="Tropical Cyclone")

        track = CycloneTrack.objects.filter(cyclone_name="Harold", country=to).first()
        if track is None:
            with open(TRACK_CSV_PATH, "rb") as f:
                csv_bytes = f.read()
            track = CycloneTrack(cyclone_name="Harold", country=to)
            track.track_file.save("to_harold_track.csv", ContentFile(csv_bytes), save=True)
            track.refresh_from_db()

        if not track.geometry_computed:
            self.stdout.write(
                self.style.ERROR(
                    f"CycloneTrack {track.id}'s track CSV failed to parse into geometry -- "
                    "check cyclone_track.utils.cyclone_csv_to_geojson against the source file."
                )
            )
            return

        hazards = HazardInformation.objects.filter(
            id__in=HAROLD_HAZARD_INFORMATION_IDS, country=to
        )
        if hazards.count() != len(HAROLD_HAZARD_INFORMATION_IDS):
            self.stdout.write(
                self.style.WARNING(
                    f"Expected {len(HAROLD_HAZARD_INFORMATION_IDS)} matching HazardInformation rows, "
                    f"found {hazards.count()} -- hazard ids may have changed since this command was "
                    "written. Proceeding with whatever matched."
                )
            )

        event = Event.objects.create(
            title=EVENT_TITLE,
            event_datetime=EVENT_DATETIME,
            event_type=event_type,
            country=to,
            cyclone_track=track,
        )
        event.hazards.set(hazards)

        self.stdout.write(
            self.style.SUCCESS(
                f"Created Event {event.id} ({EVENT_TITLE}), CycloneTrack {track.id} "
                f"({len(track.geometry['features'])} track points, no per-point timestamps), "
                f"linked to {hazards.count()} hazard row(s)."
            )
        )
