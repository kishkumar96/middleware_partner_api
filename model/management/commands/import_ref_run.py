"""Load the outputs of a Rapid Exposure Forecast (REF) run as an event.

REF runs on the RiskScape platform. Until its runs are fetched automatically
(see runners/README.md), this loads a run's downloaded output folder so the
dashboard can show it:

    python manage.py import_ref_run /data/ref-run-20365 --country VU \\
        --title "TC Lola - Rapid exposure forecast" --track 2

Each output the dashboard reads becomes a risk_information row, and a new
Event for the country links them to the cyclone track. Platform downloads
prefix every file with the run name ("test track-69-regional-summary.csv");
the prefix is dropped so the dashboard recognises each file by its name.

Without --track, a cyclone track is created from the run's
cyclone-track.geojson. Re-running with the same title fails unless --replace
is given, which removes the earlier import first.
"""

import json
import sqlite3
from datetime import timezone as dt_timezone
from pathlib import Path

from django.core.files.base import ContentFile
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from country.lookup import find_country
from cyclone_track.models import CycloneTrack
from cyclone_track_type.models import CycloneTrackType
from event.models import Event
from event_type.models import EventType
from risk_information.models import RiskInformation

from ...models import Model, ModelDomain, ModelPriority, ModelRun
from ...queueing import publish_run_to_event

# The REF outputs the dashboard reads, by the name they end with
REF_OUTPUTS = (
    "regional-impacts.geojson",
    "regional-summary.csv",
    "total-exposed-by-country.csv",
    "total-by-windspeed.csv",
    "total-by-track-distance.csv",
    "exposure-by-cluster.geojson",
)
# Map overlays, loaded when the run has them
OPTIONAL_OUTPUTS = (
    # Preserve the exact geometry supplied to REF even when --track points at
    # the Partner API's source track, so the dashboard can render the run's
    # reproducible input rather than an assumed equivalent track.
    "cyclone-track.geojson",
    "wind-swaths.geojson",
    "track-distance.geojson",
    "floodplain-exposure.geojson",
)
TRACK_OUTPUT = "cyclone-track.geojson"

DATASET_TYPES = {
    "regional-impacts.geojson": "regional_impacts",
    "regional-summary.csv": "regional_summary",
    "total-exposed-by-country.csv": "forecast_national",
    "total-by-windspeed.csv": "forecast_by_wind_speed",
    "total-by-track-distance.csv": "forecast_by_track_distance",
    "exposure-by-cluster.geojson": "exposure_by_cluster",
    "cyclone-track.geojson": "forecast_cyclone_track",
    "wind-swaths.geojson": "forecast_wind_swaths",
    "track-distance.geojson": "forecast_track_distance",
    "floodplain-exposure.geojson": "forecast_floodplain",
}


def find_output(folder: Path, name: str) -> Path | None:
    matches = sorted(p for p in folder.iterdir() if p.is_file() and p.name.endswith(name))
    if len(matches) > 1:
        raise CommandError(f"More than one file ends with {name}: {', '.join(p.name for p in matches)}")
    return matches[0] if matches else None


def track_points(path: Path) -> dict:
    """The run's track line as the point FeatureCollection the API serves for
    tracks read from CSV."""
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    coordinates = []
    for feature in data.get("features") or []:
        geometry = feature.get("geometry") or {}
        if geometry.get("type") == "LineString":
            coordinates.extend(geometry.get("coordinates") or [])
        elif geometry.get("type") == "MultiLineString":
            for line in geometry.get("coordinates") or []:
                coordinates.extend(line)
    if not coordinates:
        raise CommandError(f"{path.name} has no track line.")
    return {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {"Latitude": lat, "Longitude": lon},
                "geometry": {"type": "Point", "coordinates": [lon, lat]},
            }
            for lon, lat, *_ in coordinates
        ],
    }


def is_empty_feature_collection(path: Path) -> bool:
    """A GeoJSON overlay with no features, e.g. no floodplain mapped for the country."""
    if path.suffix.lower() not in (".geojson", ".json"):
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except ValueError:
        return False
    return isinstance(data, dict) and data.get("type") == "FeatureCollection" and not data.get("features")


def track_output_is_empty(folder: Path) -> bool | None:
    """Whether the run's own track output holds no track.

    A run whose model dropped every track point still finishes as complete, and
    its tables look like "no exposure". A real track that misses everything
    still has its line here, so an empty track output means the run failed.
    None when the folder has no track output to look at.
    """
    geojson = find_output(folder, TRACK_OUTPUT)
    if geojson is not None:
        data = json.loads(geojson.read_text(encoding="utf-8-sig"))
        return not any((f.get("geometry") or {}).get("coordinates") for f in data.get("features") or [])
    gpkg = find_output(folder, "cyclone-track.gpkg")
    if gpkg is not None:
        with sqlite3.connect(gpkg) as db:
            tables = [row[0] for row in db.execute("SELECT table_name FROM gpkg_contents")]
            return all(db.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0] == 0 for table in tables)
    return None


class Command(BaseCommand):
    help = "Load a Rapid Exposure Forecast run's output folder as an event."

    def add_arguments(self, parser):
        parser.add_argument("folder", help="Folder holding the run's downloaded outputs.")
        parser.add_argument("--country", required=True, help="Country code, e.g. VU (a name also works).")
        parser.add_argument("--title", required=True, help="Event title shown on the dashboard.")
        parser.add_argument("--track", type=int, help="Existing cyclone track id the run used.")
        parser.add_argument("--track-name", help="Name for a track created from the run's track line.")
        parser.add_argument(
            "--event-time",
            help="Event date and time (ISO 8601). Defaults to the track's issued time, else now.",
        )
        parser.add_argument("--model-run", type=int, help="Model run these outputs came from, if any.")
        parser.add_argument("--replace", action="store_true", help="Replace an earlier import with this title.")
        parser.add_argument(
            "--as-run",
            action="store_true",
            help=(
                "Record this as one advisory run of the storm's Event (one Event per storm and country, "
                "as the automated runner keeps it), not as an Event of its own. Needs --track."
            ),
        )
        parser.add_argument("--external-url", default="", help="Where the run lives, e.g. its RiskScape run page.")

    def handle(self, *args, **options):
        folder = Path(options["folder"])
        if not folder.is_dir():
            raise CommandError(f"Not a folder: {folder}")
        country = find_country(options["country"])
        if country is None:
            raise CommandError(f"No country matching '{options['country']}'. Give its code, e.g. CK.")
        event_type = EventType.objects.filter(event_name__iexact="Tropical Cyclone").first()
        if event_type is None:
            raise CommandError("No 'Tropical Cyclone' event type.")
        model_run = None
        if options["model_run"]:
            model_run = ModelRun.objects.filter(pk=options["model_run"]).first()
            if model_run is None:
                raise CommandError(f"No model run {options['model_run']}.")

        outputs = {name: find_output(folder, name) for name in REF_OUTPUTS}
        missing = [name for name, path in outputs.items() if path is None]
        if missing:
            raise CommandError(f"Not a REF output folder, missing: {', '.join(missing)}")
        if track_output_is_empty(folder):
            raise CommandError(
                "The run's cyclone-track output has no track, so the model dropped every track point "
                "and its empty tables mean a failed run, not zero exposure. Check the run's warnings."
            )
        skipped = []
        for name in OPTIONAL_OUTPUTS:
            path = find_output(folder, name)
            if path is None:
                continue
            # An overlay with nothing in it (no floodplain for the country) is a
            # real result; it just has nothing to draw, so don't load it
            if is_empty_feature_collection(path):
                skipped.append(name)
                continue
            outputs[name] = path

        event_time = None
        if options["event_time"]:
            event_time = parse_datetime(options["event_time"])
            if event_time is None:
                raise CommandError(f"Can't read --event-time {options['event_time']}.")
            if timezone.is_naive(event_time):
                event_time = timezone.make_aware(event_time, dt_timezone.utc)

        title = options["title"]
        if options["as_run"]:
            if not options["track"]:
                raise CommandError("--as-run needs --track: the advisory's track says which storm it belongs to.")
            event, run = self.import_as_run(options, country, outputs, title)
            if skipped:
                self.stdout.write(f"Skipped empty overlays: {', '.join(skipped)}.")
            shows = "now the run the Event shows" if event.model_run_id == run.pk else "kept alongside a newer run"
            self.stdout.write(
                self.style.SUCCESS(
                    f"Run {run.pk} '{title}' for {country.short_name}: Event {event.pk} '{event.title}', "
                    f"track {run.tc_track_id}, {run.risk_information.count()} outputs, {shows}."
                )
            )
            return
        with transaction.atomic():
            earlier = Event.objects.filter(country=country, title=title)
            if earlier.exists():
                if not options["replace"]:
                    raise CommandError(f"{country.short_name} already has an event '{title}'. Pass --replace.")
                for event in earlier:
                    self.remove(event)

            track = self.get_track(options, folder, country)
            event = Event.objects.create(
                title=title,
                event_datetime=event_time or track.issued_time or timezone.now(),
                event_type=event_type,
                country=country,
                cyclone_track=track,
                model_run=model_run,
            )
            for name, path in outputs.items():
                risk = RiskInformation(
                    title=f"{Path(name).stem.replace('-', ' ')} (REF)",
                    dataset_type=DATASET_TYPES[name],
                    country=country,
                    model_run=model_run,
                )
                risk.upload.save(name, ContentFile(path.read_bytes()), save=True)
                risk.refresh_from_db()
                if not risk.geometry_computed:
                    raise CommandError(f"{path.name} couldn't be read.")
                event.risks.add(risk)

        if skipped:
            self.stdout.write(f"Skipped empty overlays: {', '.join(skipped)}.")
        self.stdout.write(
            self.style.SUCCESS(
                f"Event {event.pk} '{title}' for {country.short_name}: track {track.pk}, "
                f"{len(outputs)} outputs ({', '.join(str(r.pk) for r in event.risks.order_by('id'))})."
            )
        )

    IMPORT_MODEL_NAME = "Rapid exposure forecast (imported)"

    def import_as_run(self, options, country, outputs, title):
        """One advisory as a completed run under the storm's single Event."""
        track = CycloneTrack.objects.filter(pk=options["track"]).first()
        if track is None:
            raise CommandError(f"No cyclone track {options['track']}.")
        with transaction.atomic():
            earlier = ModelRun.objects.filter(tc_track=track, models__name=self.IMPORT_MODEL_NAME)
            if earlier.exists():
                if not options["replace"]:
                    raise CommandError(
                        f"Track {track.pk} was already imported as run {earlier.first().pk}. Pass --replace."
                    )
                for old in earlier:
                    self.remove_run(old)

            model, _ = Model.objects.get_or_create(
                name=self.IMPORT_MODEL_NAME, defaults={"server_name": "manual-import"}
            )
            run = ModelRun.objects.create(
                name=title,
                model_domain=ModelDomain.objects.get_or_create(name="Tropical cyclone")[0],
                tc_track=track,
                priority=ModelPriority.objects.get_or_create(name="High")[0],
                status=ModelRun.COMPLETE,
                model_run_completed=True,
                finished_at=timezone.now(),
                external_url=options["external_url"],
            )
            run.models.add(model)
            for name, path in outputs.items():
                risk = RiskInformation(
                    title=f"{Path(name).stem.replace('-', ' ')} (REF)",
                    dataset_type=DATASET_TYPES[name],
                    country=country,
                    model_run=run,
                )
                risk.upload.save(name, ContentFile(path.read_bytes()), save=True)
                risk.refresh_from_db()
                if not risk.geometry_computed:
                    raise CommandError(f"{path.name} couldn't be read.")
            event = publish_run_to_event(run, country_id=country.pk)
        return event, run

    def remove_run(self, run: ModelRun):
        """An earlier as-run import: its outputs and the run itself (the Event keeps its other runs)."""
        for risk in list(run.risk_information.all()):
            risk.upload.delete(save=False)
            risk.delete()
        run.delete()

    def get_track(self, options, folder: Path, country) -> CycloneTrack:
        if options["track"]:
            track = CycloneTrack.objects.filter(pk=options["track"]).first()
            if track is None:
                raise CommandError(f"No cyclone track {options['track']}.")
            return track
        path = find_output(folder, TRACK_OUTPUT)
        if path is None:
            raise CommandError(f"No {TRACK_OUTPUT} in the folder; pass --track.")
        geometry = track_points(path)
        track = CycloneTrack(
            cyclone_name=options["track_name"] or options["title"],
            country=country,
            cyclone_track_type=CycloneTrackType.objects.filter(name__icontains="forecast").first(),
        )
        track.track_file.save(TRACK_OUTPUT, ContentFile(path.read_bytes()), save=True)
        # The API reads geometry from track CSVs only; set the run's line here
        CycloneTrack.objects.filter(pk=track.pk).update(geometry=geometry, geometry_computed=True)
        track.refresh_from_db()
        return track

    def remove(self, event: Event):
        """An earlier import: its event, its outputs, and the track it created."""
        track = event.cyclone_track
        risks = list(event.risks.all())
        event.delete()
        for risk in risks:
            if not risk.events.exists():
                risk.upload.delete(save=False)
                risk.delete()
        created_here = (
            track is not None and Path(track.track_file.name).name.startswith(Path(TRACK_OUTPUT).stem)
        )
        if created_here and not track.events.exists():
            track.track_file.delete(save=False)
            track.delete()
