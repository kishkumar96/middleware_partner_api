"""Model run queue: cyclone tracks in, runner jobs out, results onto Events.

Flow:
  1. A CycloneTrack is posted -> queue_runs_for_track() creates one QUEUED
     ModelRun per matching ModelTrigger, and supersedes older queued runs for
     the same storm.
  2. A runner calls claim_next_run(server_name) -> the oldest QUEUED run whose
     model lives on that server becomes RUNNING.
  3. The runner uploads its outputs as RiskInformation rows tagged with the
     run, then calls finish_run() -> COMPLETE runs are published to the
     storm's Event, which is what the dashboard reads.
"""

import datetime
import logging
import re

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from event.models import Event
from event_type.models import EventType

from .models import ModelRun, ModelTrigger

logger = logging.getLogger(__name__)

# A runner that claimed a run and went quiet for this long is assumed dead,
# and the run goes back on the queue (up to MAX_ATTEMPTS claims in total).
LEASE_SECONDS = getattr(settings, "MODEL_RUN_LEASE_SECONDS", 2 * 60 * 60)
MAX_ATTEMPTS = getattr(settings, "MODEL_RUN_MAX_ATTEMPTS", 3)

# Advisories and the best track for one storm are only treated as the same
# Event when they are this close together; storm names get reused.
SAME_STORM_WINDOW = datetime.timedelta(days=60)

# SPC names tracks like 2026040812_VAIANU: <issue YYYYMMDDHH>_<storm>
_ISSUE_PREFIX = re.compile(r"^\d{8,12}_(?P<storm>.+)$")


def storm_name(cyclone_name: str | None) -> str:
    """The storm's own name from an SPC cyclone_name, e.g. 'VAIANU'."""
    name = (cyclone_name or "").strip()
    match = _ISSUE_PREFIX.match(name)
    return match.group("storm") if match else name


def is_best_track(track) -> bool:
    # Track types are rows, not an enum; SPC's is "Post-event Best Track"
    track_type = track.cyclone_track_type
    return bool(track_type and "best" in track_type.name.lower())


def _authority(track):
    """Higher wins: a best track beats any forecast, then newer beats older."""
    issued = track.issued_time or datetime.datetime.min.replace(tzinfo=datetime.timezone.utc)
    return (is_best_track(track), issued)


def _same_storm(track_a, track_b) -> bool:
    if storm_name(track_a.cyclone_name).lower() != storm_name(track_b.cyclone_name).lower():
        return False
    if track_a.issued_time and track_b.issued_time:
        return abs(track_a.issued_time - track_b.issued_time) <= SAME_STORM_WINDOW
    return True


@transaction.atomic
def queue_runs_for_track(track) -> list[ModelRun]:
    """Queue a run for every enabled trigger matching this track."""
    if not track.cyclone_track_type_id:
        return []

    triggers = (
        ModelTrigger.objects.filter(enabled=True, cyclone_track_type=track.cyclone_track_type_id)
        .filter(Q(country__isnull=True) | Q(country=track.country_id))
        .select_related("model")
    )

    created = []
    for trigger in triggers:
        if ModelRun.objects.filter(tc_track=track, models=trigger.model).exists():
            continue
        run = ModelRun.objects.create(
            name=f"{trigger.model.name}: {track.cyclone_name or f'track {track.pk}'}",
            model_domain=trigger.model_domain,
            tc_track=track,
            priority=trigger.priority,
        )
        # The whole chain, so the runner executes every step (see Model.next_model)
        run.models.add(*trigger.model.chain())
        created.append(run)
        _supersede_older_runs(run, trigger.model)

    if created:
        logger.info("Queued model runs %s for cyclone track %s", [r.pk for r in created], track.pk)
    return created


def _supersede_older_runs(new_run: ModelRun, model) -> None:
    # Only runs nobody has started yet: a running one is left to finish, and
    # publish_run_to_event() keeps it from overwriting newer results.
    track = new_run.tc_track
    older = (
        ModelRun.objects.filter(status=ModelRun.QUEUED, models=model, tc_track__country=track.country_id)
        .exclude(pk=new_run.pk)
        .select_related("tc_track__cyclone_track_type")
    )
    for run in older:
        if _same_storm(run.tc_track, track) and _authority(run.tc_track) < _authority(track):
            run.status = ModelRun.SUPERSEDED
            run.finished_at = timezone.now()
            run.error = f"Superseded by run {new_run.pk} (cyclone track {track.pk})."
            run.save(update_fields=["status", "finished_at", "error"])


def ordered_run_models(run: ModelRun) -> list:
    """The run's models in execution order: the chain starting at the one
    model no other model in the run points to. Models outside that chain
    (added by hand) follow, by id."""
    models = list(run.models.all())
    if len(models) <= 1:
        return models
    in_run = {m.pk for m in models}
    pointed_to = {m.next_model_id for m in models if m.next_model_id in in_run}
    heads = sorted((m for m in models if m.pk not in pointed_to), key=lambda m: m.pk)
    head = heads[0] if heads else min(models, key=lambda m: m.pk)
    ordered = [m for m in head.chain() if m.pk in in_run]
    seen = {m.pk for m in ordered}
    return ordered + sorted((m for m in models if m.pk not in seen), key=lambda m: m.pk)


@transaction.atomic
def claim_next_run(server_name: str) -> ModelRun | None:
    """Hand the oldest queued run for this server to a runner, or None."""
    now = timezone.now()
    for_server = ModelRun.objects.filter(models__server_name=server_name)

    # Recover runs whose runner died mid-run
    stale = for_server.filter(status=ModelRun.RUNNING, claimed_at__lt=now - datetime.timedelta(seconds=LEASE_SECONDS))
    stale.filter(attempts__gte=MAX_ATTEMPTS).update(
        status=ModelRun.FAILED,
        finished_at=now,
        error=f"No result after {MAX_ATTEMPTS} attempts.",
    )
    stale.filter(attempts__lt=MAX_ATTEMPTS).update(status=ModelRun.QUEUED)

    run = (
        ModelRun.objects.select_for_update(skip_locked=True, of=("self",))
        .filter(pk__in=for_server.filter(status=ModelRun.QUEUED).values("pk"))
        .order_by("id")
        .first()
    )
    if run is None:
        return None

    run.status = ModelRun.RUNNING
    run.claimed_by = server_name
    run.claimed_at = now
    run.attempts += 1
    run.error = ""
    run.save(update_fields=["status", "claimed_by", "claimed_at", "attempts", "error"])
    return run


@transaction.atomic
def finish_run(run: ModelRun, status: str, error: str = "", external_url: str = "") -> Event | None:
    """Record a runner's result; publish it if the run completed."""
    if status not in (ModelRun.COMPLETE, ModelRun.FAILED):
        raise ValueError(f"cannot finish a run as {status!r}")

    run.status = status
    run.model_run_completed = status == ModelRun.COMPLETE
    run.finished_at = timezone.now()
    run.error = error
    if external_url:
        run.external_url = external_url
    run.save(update_fields=["status", "model_run_completed", "finished_at", "error", "external_url"])

    if status == ModelRun.COMPLETE:
        return publish_run_to_event(run)
    return None


def publish_run_to_event(run: ModelRun, country_id: int | None = None) -> Event | None:
    """Make this run's results the ones the storm's Event shows.

    One Event per storm and country. The run's RiskInformation rows replace
    any earlier run's on that Event, unless the Event already shows a run with
    more authority (a newer advisory, or the best track), e.g. when an old
    forecast run finishes late. Risks linked by hand (no model_run) are kept.
    """
    track = run.tc_track
    # A storm's track belongs to one country, but its results can be for another (SPC files a
    # track under the country it was issued for); the run's own country wins when given.
    country_id = country_id or _results_country_id(run) or track.country_id
    if not country_id:
        logger.warning("Model run %s: cyclone track %s has no country, not publishing", run.pk, track.pk)
        return None

    event = _event_for_storm(track, country_id)
    if event is None:
        event_type, _ = EventType.objects.get_or_create(event_name="Tropical Cyclone")
        event = Event.objects.create(
            title=f"Tropical Cyclone {storm_name(track.cyclone_name).title()}",
            event_datetime=track.issued_time,
            event_type=event_type,
            country_id=country_id,
            cyclone_track=track,
        )
    elif event.model_run_id and _authority(event.model_run.tc_track) > _authority(track):
        logger.info("Model run %s: Event %s already shows newer run %s", run.pk, event.pk, event.model_run_id)
        return event

    event.cyclone_track = track
    event.model_run = run
    event.save(update_fields=["cyclone_track", "model_run"])
    event.risks.remove(*event.risks.filter(model_run__isnull=False))
    event.risks.add(*run.risk_information.all())
    return event


def forecast_runs_for_event(event: Event) -> list[dict]:
    """Every forecast run for the Event's storm, newest advisory first.

    The Event shows one run at a time (publish_run_to_event keeps only the most
    authoritative), but each run keeps its own outputs, so earlier advisories
    can still be looked at. Events with no model runs (loaded by hand) get [].
    """
    track = event.cyclone_track
    if track is None:
        return []
    runs = (
        ModelRun.objects.filter(
            Q(tc_track__country=event.country_id) | Q(risk_information__country=event.country_id),
            models__name__iregex=r"rapid[ _-]?exposure",
        )
        .select_related("tc_track")
        .prefetch_related("risk_information")
        .distinct()
    )
    out = []
    for run in runs:
        if not _same_storm(track, run.tc_track):
            continue
        out.append(
            {
                "id": run.pk,
                "cyclone_track": run.tc_track_id,
                "issued_time": run.tc_track.issued_time,
                "status": run.status,
                "finished_at": run.finished_at,
                "error": run.error,
                "external_url": run.external_url,
                "risks": sorted(risk.pk for risk in run.risk_information.all()),
                "is_current": event.model_run_id == run.pk,
            }
        )
    out.sort(key=lambda r: (r["issued_time"] is not None, r["issued_time"], r["id"]), reverse=True)
    return out


def _results_country_id(run: ModelRun) -> int | None:
    """The country most of the run's outputs are for, if they say.

    A storm's track is filed under one country (SPC filed TC Wati under Fiji), but the
    exposure it produced is for wherever the storm actually reaches (Cook Islands).
    """
    counts: dict[int, int] = {}
    for country_id in run.risk_information.values_list("country_id", flat=True):
        if country_id:
            counts[country_id] = counts.get(country_id, 0) + 1
    return max(counts, key=counts.get) if counts else None


def _event_for_storm(track, country_id: int | None = None) -> Event | None:
    events = Event.objects.filter(
        country=country_id or track.country_id, cyclone_track__isnull=False
    ).select_related(
        "cyclone_track", "model_run__tc_track__cyclone_track_type"
    )
    for event in events.order_by("-id"):
        if not _same_storm(event.cyclone_track, track):
            continue
        # Imported historical tracks often have no issued_time; fall back to
        # the Event's own date so a reused storm name doesn't match them.
        if event.event_datetime and track.issued_time:
            if abs(event.event_datetime - track.issued_time) > SAME_STORM_WINDOW:
                continue
        return event
    return None
