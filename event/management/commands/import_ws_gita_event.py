"""Imports a real Event for Samoa's Tropical Cyclone Gita (February 2018),
third country in the Event/CycloneTrack population effort (see
import_vu_lola_event.py for background).

PROVENANCE WARNING, unlike Lola/Harold: this command's track data is
lower-confidence than the other two, and was only imported after the
project owner was explicitly told that and chose to proceed anyway (this
docstring exists so that decision is visible here too, not just in a
chat transcript). Recovered from public/samoa/cyclone-track-gita.geojson
in Partner2's git history (deleted alongside Lola's and Harold's source
files in the same partner-API migration) -- but unlike those two files,
this one carries ZERO properties (no name, no event label, nothing
identifying it as Gita specifically) and its 125 points span an
unusually vast area (roughly 10S-15S near Samoa down to ~49S, near
sub-Antarctic latitudes).

That said, it is NOT necessarily implausible: Gita's remnants underwent
a well-documented, widely-reported explosive extratropical
intensification south of New Zealand in late Feb 2018, and this file's
shape (Samoa area -> past Fiji/Tonga -> curving southeast, ending near
49S/165W) is consistent with that real event's public-record path. But
the file itself carries no independent confirmation, and no attempt was
made in this session to cross-check it against an authoritative track
archive (IBTrACS/RSMC Nadi) -- no internet access to those was available.
Treat cyclone_track.geometry here as best-effort, not verified, until
someone can check it against such an archive.

The two linked HazardInformation rows (9413/9414, "TC Gita Flood
Hazard"/"Torical cyclone Gita wind hazard" [sic, typo in source title])
ARE independently confirmed real: they're the only WS rows whose title
or THREDDS URL mentions "gita", both pointing to real, live
gemthreddshpc.spc.int datasets under case_study2/Gita_WS/. Those are not
in question -- only the track geometry's precise shape is.

EVENT_DATETIME uses Gita's well-documented formation-near-Samoa date
(2018-02-09) from public record, not derived from this dataset (which,
like Harold's, has no per-point timestamps -- every Time cell is
deliberately left blank rather than invented).

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

TRACK_CSV_PATH = Path(__file__).resolve().parent / "data" / "ws_gita_track.csv"
EVENT_TITLE = "Tropical Cyclone Gita"
# Approximate: Gita's documented formation date near Samoa, not derived
# from this track file (no per-point timestamps). See module docstring
# for the broader provenance caveat on this event's track data.
EVENT_DATETIME = datetime.datetime(2018, 2, 9, 0, 0, 0, tzinfo=datetime.timezone.utc)
GITA_HAZARD_INFORMATION_IDS = [9413, 9414]


class Command(BaseCommand):
    help = (
        "Import the Tropical Cyclone Gita Event + CycloneTrack for Samoa. "
        "Track geometry is best-effort/unconfirmed -- see module docstring."
    )

    def handle(self, *args, **options):
        ws = Country.objects.get(short_name="WS")

        if Event.objects.filter(country=ws, title=EVENT_TITLE).exists():
            self.stdout.write(self.style.WARNING(f"{EVENT_TITLE} event already exists, skipping."))
            return

        event_type, _ = EventType.objects.get_or_create(event_name="Tropical Cyclone")

        track = CycloneTrack.objects.filter(cyclone_name="Gita", country=ws).first()
        if track is None:
            with open(TRACK_CSV_PATH, "rb") as f:
                csv_bytes = f.read()
            track = CycloneTrack(cyclone_name="Gita", country=ws)
            track.track_file.save("ws_gita_track.csv", ContentFile(csv_bytes), save=True)
            track.refresh_from_db()

        if not track.geometry_computed:
            self.stdout.write(
                self.style.ERROR(
                    f"CycloneTrack {track.id}'s track CSV failed to parse into geometry -- "
                    "check cyclone_track.utils.cyclone_csv_to_geojson against the source file."
                )
            )
            return

        hazards = HazardInformation.objects.filter(id__in=GITA_HAZARD_INFORMATION_IDS, country=ws)
        if hazards.count() != len(GITA_HAZARD_INFORMATION_IDS):
            self.stdout.write(
                self.style.WARNING(
                    f"Expected {len(GITA_HAZARD_INFORMATION_IDS)} matching HazardInformation rows, "
                    f"found {hazards.count()} -- hazard ids may have changed since this command was "
                    "written. Proceeding with whatever matched."
                )
            )

        event = Event.objects.create(
            title=EVENT_TITLE,
            event_datetime=EVENT_DATETIME,
            event_type=event_type,
            country=ws,
            cyclone_track=track,
        )
        event.hazards.set(hazards)

        self.stdout.write(
            self.style.SUCCESS(
                f"Created Event {event.id} ({EVENT_TITLE}), CycloneTrack {track.id} "
                f"({len(track.geometry['features'])} track points, geometry UNCONFIRMED -- "
                f"see module docstring), linked to {hazards.count()} hazard row(s)."
            )
        )
