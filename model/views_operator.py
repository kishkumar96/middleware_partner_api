"""Operator endpoints: submit a cyclone track by hand and follow its model runs.

Superusers only. One superuser covers every country, unlike the dashboard's
per-country logins. Used by the dashboard's operator page through its server
routes (the token never reaches the browser).
"""

from django.db import transaction
from rest_framework import status
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.permissions import BasePermission, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from country.models import Country
from cyclone_track.models import CycloneTrack
from cyclone_track.serializers import CycloneTrackSerializer
from cyclone_track_type.models import CycloneTrackType

from .models import ModelRun, ModelTrigger
from .queueing import ordered_run_models, queue_runs_for_track
from .run_descriptions import explain_nothing_queued, pipeline_label, runner_status


class IsSuperUser(BasePermission):
    message = "Only superusers can submit cyclone tracks."

    def has_permission(self, request, view):
        return bool(request.user and request.user.is_authenticated and request.user.is_superuser)


def run_summary(run: ModelRun) -> dict:
    models = ordered_run_models(run)
    server = models[0].server_name if models else ""
    event = run.events.first()
    return {
        "id": run.pk,
        "status": run.status,
        "attempts": run.attempts,
        "error": run.error,
        "models": [m.name for m in models],
        "runner": runner_status(server),
        "created_at": run.created_at,
        "claimed_at": run.claimed_at,
        "finished_at": run.finished_at,
        "external_url": run.external_url,
        "logs": [entry.logs for entry in run.logs.order_by("id")],
        "event": (
            {"id": event.pk, "title": event.title, "country": event.country.short_name}
            if event
            else None
        ),
    }


class OperatorMeView(APIView):
    """Who the token belongs to, and whether they may submit tracks."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response({"username": request.user.get_username(), "is_superuser": request.user.is_superuser})


class OperatorPipelinesView(APIView):
    """What can be submitted: each track type, the pipeline it starts, and
    whether that pipeline's runner is online; plus the countries."""

    permission_classes = [IsSuperUser]

    def get(self, request):
        track_types = []
        for track_type in CycloneTrackType.objects.order_by("id"):
            triggers = ModelTrigger.objects.filter(cyclone_track_type=track_type, enabled=True).select_related(
                "model", "country"
            )
            track_types.append(
                {
                    "id": track_type.pk,
                    "name": track_type.name,
                    "label": pipeline_label(track_type),
                    "pipelines": [
                        {
                            "country": trigger.country.short_name if trigger.country_id else None,
                            "models": [m.name for m in trigger.model.chain()],
                            "runner": runner_status(trigger.model.server_name),
                        }
                        for trigger in triggers
                    ],
                }
            )
        countries = [
            {"id": c.pk, "code": c.short_name, "name": c.long_name}
            for c in Country.objects.order_by("long_name")
        ]
        return Response({"track_types": track_types, "countries": countries})


class OperatorSubmitTrackView(APIView):
    """Create a cyclone track and queue its model runs straight away.

    Rejects a file with no readable track points instead of queueing a run
    that would only fail later.
    """

    permission_classes = [IsSuperUser]
    parser_classes = (MultiPartParser, FormParser)

    def post(self, request):
        serializer = CycloneTrackSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        with transaction.atomic():
            track: CycloneTrack = serializer.save(notify=False)
            track.refresh_from_db()
            features = (track.geometry or {}).get("features") or []
            if not track.geometry_computed or not features:
                track.track_file.delete(save=False)
                track.delete()
                return Response(
                    {"detail": "No track points could be read from this file. Check it's an RSMC-style CSV "
                               "with time, latitude and longitude columns."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            runs = queue_runs_for_track(track)

        message = None if runs else explain_nothing_queued(track)
        return Response(
            {
                "track": {"id": track.pk, "cyclone_name": track.cyclone_name, "points": len(features)},
                "runs": [run_summary(run) for run in runs],
                "message": message,
            },
            status=status.HTTP_201_CREATED,
        )


class OperatorRunsView(APIView):
    """The model runs for one track (?track=<id>), with status, logs and event."""

    permission_classes = [IsSuperUser]

    def get(self, request):
        track_id = request.query_params.get("track")
        if not track_id or not track_id.isdigit():
            return Response({"detail": "Pass ?track=<id>."}, status=status.HTTP_400_BAD_REQUEST)
        runs = ModelRun.objects.filter(tc_track_id=int(track_id)).order_by("id")
        return Response({"runs": [run_summary(run) for run in runs]})
