from rest_framework.generics import ListCreateAPIView, RetrieveAPIView
from rest_framework.response import Response
from rest_framework.exceptions import NotFound
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.permissions import IsAuthenticatedOrReadOnly
from rest_framework.exceptions import APIException
from django.db import transaction
import logging

from .model_input import model_ready_geometry
from .models import CycloneTrack
from .serializers import CycloneTrackSerializer
from mailer.utils import SPCMailer
from model.queueing import queue_runs_for_track


logger = logging.getLogger(__name__)


class CycloneTrackListCreateView(ListCreateAPIView):
    queryset = CycloneTrack.objects.all().order_by("-id")
    serializer_class = CycloneTrackSerializer

    # GET is public, POST requires auth
    permission_classes = (IsAuthenticatedOrReadOnly,)

    # Needed for file upload
    parser_classes = (MultiPartParser, FormParser)

    def perform_create(self, serializer):
        with transaction.atomic():
            instance: CycloneTrack = serializer.save()

            if instance.notify and not instance.mail_configuration_id:
                # This should already be enforced by serializer validation, but keep it here
                # as a defensive guard.
                raise APIException("mail_configuration is required when notify=true")

            def _send_notification_best_effort(track_id: int) -> None:
                try:
                    track = CycloneTrack.objects.select_related("mail_configuration").get(
                        pk=track_id
                    )
                    if not track.notify:
                        return
                    if not track.mail_configuration_id:
                        logger.warning(
                            "CycloneTrack notify=true but mail_configuration is missing; skipping email. track_id=%s",
                            track_id,
                        )
                        return

                    result = SPCMailer.send_with_config_sync(track.mail_configuration)
                    if not result.ok:
                        logger.error(
                            "CycloneTrack email send failed; continuing without failing request. track_id=%s status=%s response=%s",
                            track_id,
                            result.status_code,
                            (result.response_text or "")[:2000],
                        )
                except Exception:
                    # Never let email/Graph issues break cyclone track ingestion.
                    logger.exception(
                        "CycloneTrack email send raised exception; continuing without failing request. track_id=%s",
                        track_id,
                    )

            # Attempt email only after the DB commit succeeds.
            transaction.on_commit(lambda: _send_notification_best_effort(instance.pk))

            # Queue impact model runs for this track (see model.ModelTrigger).
            # Only tracks posted through the API: management-command imports
            # of historical storms shouldn't start model runs.
            def _queue_runs_best_effort(track_id: int) -> None:
                try:
                    queue_runs_for_track(CycloneTrack.objects.get(pk=track_id))
                except Exception:
                    logger.exception("Queueing model runs failed. track_id=%s", track_id)

            transaction.on_commit(lambda: _queue_runs_best_effort(instance.pk))


class CycloneTrackRetrieveView(RetrieveAPIView):
    queryset = CycloneTrack.objects.all()
    serializer_class = CycloneTrackSerializer

    # GET is public (same as list)
    permission_classes = (IsAuthenticatedOrReadOnly,)


class CycloneTrackModelInputView(RetrieveAPIView):
    """The track as a GeoJSON FeatureCollection the impact models can read (ISO-UTC times)."""

    queryset = CycloneTrack.objects.all()
    permission_classes = (IsAuthenticatedOrReadOnly,)

    def retrieve(self, request, *args, **kwargs):
        track = self.get_object()
        if not track.geometry:
            raise NotFound("This cyclone track has no computed geometry.")
        return Response(model_ready_geometry(track.geometry))
