"""Plain-language descriptions of model runs, for the admin and the operator page.

Say which pipeline a track started and whether its runner is there to pick
it up, or why nothing started, instead of a bare count.
"""

from datetime import timedelta

from django.db.models import Q
from django.utils import timezone

from .models import ModelRun, ModelTrigger, RunnerHeartbeat
from .queueing import ordered_run_models

RUNNER_FRESHNESS = timedelta(minutes=2)


def pipeline_label(track_type) -> str:
    if track_type is None:
        return "Model"
    # Same rule as queueing.is_best_track, applied to the type itself
    if "best" in track_type.name.lower():
        return "PDNA (post-event best track)"
    if "forecast" in track_type.name.lower():
        return "Rapid (forecast)"
    return track_type.name


def runner_status(server_name: str) -> dict:
    heartbeat = RunnerHeartbeat.objects.filter(server_name=server_name).first()
    online = bool(heartbeat and heartbeat.last_seen_at >= timezone.now() - RUNNER_FRESHNESS)
    return {
        "server_name": server_name,
        "online": online,
        "last_seen_at": heartbeat.last_seen_at if heartbeat else None,
    }


def track_label(track) -> str:
    return track.cyclone_name or f"track {track.pk}"


def describe_queued_run(run: ModelRun) -> str:
    models = ordered_run_models(run)
    server = models[0].server_name if models else "?"
    when = (
        "its runner is online"
        if runner_status(server)["online"]
        else "its runner is offline, so it starts when the runner checks in"
    )
    chain = " → ".join(m.name for m in models)
    return (
        f"Queued {pipeline_label(run.tc_track.cyclone_track_type)} run {run.pk} for "
        f"{track_label(run.tc_track)} ({chain}) on {server}; {when}."
    )


def explain_nothing_queued(track) -> str:
    """Why queueing this track started no run."""
    name = track_label(track)
    if not track.geometry_computed:
        return f"Nothing queued for {name}: no track points could be read from its file."
    if not track.cyclone_track_type_id:
        return f"Nothing queued for {name}: it has no track type, and the type decides the pipeline."
    triggers = ModelTrigger.objects.filter(enabled=True, cyclone_track_type=track.cyclone_track_type_id).filter(
        Q(country__isnull=True) | Q(country=track.country_id)
    )
    if not triggers.exists():
        where = track.country.long_name if track.country_id else "this country"
        return (
            f"Nothing queued for {name}: no enabled pipeline runs {track.cyclone_track_type} tracks "
            f"for {where}. Add a model trigger."
        )
    existing = (
        ModelRun.objects.filter(tc_track=track, models__in=[t.model_id for t in triggers])
        .distinct()
        .order_by("-id")
    )
    if existing:
        runs = ", ".join(f"run {run.pk} ({run.status})" for run in existing)
        return (
            f"Nothing queued for {name}: it already has {runs}. To run it again, select the run "
            "under Model runs and use “Requeue selected runs”."
        )
    return f"Nothing queued for {name}."
