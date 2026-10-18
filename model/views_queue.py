from datetime import timedelta

from django.db.models import Count
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import serializers, status
from rest_framework.response import Response
from rest_framework.views import APIView

from cyclone_track.serializers import CycloneTrackSerializer

from .models import Model, ModelRun, RunnerHeartbeat
from .queueing import claim_next_run, finish_run, ordered_run_models, storm_name


class QueueModelSerializer(serializers.ModelSerializer):
    class Meta:
        model = Model
        fields = ("id", "name", "location", "server_name", "inputs", "run_command")


class ClaimRequestSerializer(serializers.Serializer):
    server_name = serializers.CharField(max_length=255)


class FinishRequestSerializer(serializers.Serializer):
    status = serializers.ChoiceField(choices=[ModelRun.COMPLETE, ModelRun.FAILED])
    error = serializers.CharField(required=False, allow_blank=True, default="")
    external_url = serializers.URLField(required=False, allow_blank=True, default="")


class HeartbeatRequestSerializer(serializers.Serializer):
    server_name = serializers.CharField(max_length=255)
    backend = serializers.ChoiceField(choices=["platform", "local"])
    hostname = serializers.CharField(max_length=255, required=False, allow_blank=True, default="")


def job_payload(run: ModelRun, request) -> dict:
    """Everything a runner needs to execute a run without further lookups."""
    track = run.tc_track
    models = ordered_run_models(run)
    return {
        "id": run.pk,
        "name": run.name,
        "attempts": run.attempts,
        # "models" is the chain in execution order; "model" (its first step)
        # is kept for runners that only know single-model jobs
        "model": QueueModelSerializer(models[0] if models else None).data,
        "models": QueueModelSerializer(models, many=True).data,
        "track": {
            **CycloneTrackSerializer(track, context={"request": request}).data,
            "storm_name": storm_name(track.cyclone_name),
            "track_type_name": track.cyclone_track_type.name if track.cyclone_track_type_id else None,
            "country_code": track.country.short_name if track.country_id else None,
        },
    }


class ModelRunClaimView(APIView):
    """POST {"server_name": ...} -> 200 with the next job, or 204 if none."""

    def post(self, request):
        body = ClaimRequestSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        run = claim_next_run(body.validated_data["server_name"])
        if run is None:
            return Response(status=status.HTTP_204_NO_CONTENT)
        return Response(job_payload(run, request))


class ModelRunFinishView(APIView):
    """POST {"status": "complete"|"failed", "error"?, "external_url"?}."""

    def post(self, request, pk):
        run = get_object_or_404(ModelRun, pk=pk)
        if run.status != ModelRun.RUNNING:
            return Response(
                {"detail": f"Run {run.pk} is {run.status}, not running."},
                status=status.HTTP_409_CONFLICT,
            )
        body = FinishRequestSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        event = finish_run(run, **body.validated_data)
        return Response({"id": run.pk, "status": run.status, "event": event.pk if event else None})


class RunnerHeartbeatView(APIView):
    """Record a runner's presence; called before each queue poll."""

    def post(self, request):
        body = HeartbeatRequestSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        heartbeat, _ = RunnerHeartbeat.objects.update_or_create(
            server_name=body.validated_data["server_name"],
            defaults={
                "backend": body.validated_data["backend"],
                "hostname": body.validated_data["hostname"],
            },
        )
        return Response(
            {
                "server_name": heartbeat.server_name,
                "last_seen_at": heartbeat.last_seen_at,
            }
        )


class ModelOperationsStatusView(APIView):
    """Small operator-facing readiness summary for run queues and runners."""

    def get(self, request):
        freshness_cutoff = timezone.now() - timedelta(minutes=2)
        pending_by_server = dict(
            ModelRun.objects.filter(status=ModelRun.QUEUED)
            .values("models__server_name")
            .annotate(count=Count("id"))
            .values_list("models__server_name", "count")
        )
        configured_servers = set(
            Model.objects.exclude(server_name="").values_list("server_name", flat=True)
        )
        heartbeats = {row.server_name: row for row in RunnerHeartbeat.objects.all()}
        runners = []
        for server_name in sorted(configured_servers):
            heartbeat = heartbeats.get(server_name)
            is_online = bool(heartbeat and heartbeat.last_seen_at >= freshness_cutoff)
            runners.append(
                {
                    "server_name": server_name,
                    "online": is_online,
                    "last_seen_at": heartbeat.last_seen_at if heartbeat else None,
                    "backend": heartbeat.backend if heartbeat else None,
                    "hostname": heartbeat.hostname if heartbeat else None,
                    "queued_runs": pending_by_server.get(server_name, 0),
                    "message": (
                        "Ready to claim queued runs"
                        if is_online
                        else "No runner heartbeat in the last 2 minutes"
                    ),
                }
            )
        return Response(
            {
                "runners": runners,
                "runs": dict(
                    ModelRun.objects.values("status")
                    .annotate(count=Count("id"))
                    .values_list("status", "count")
                ),
            }
        )
