from rest_framework import serializers

from model.queueing import forecast_runs_for_event

from .models import Event


class EventSerializer(serializers.ModelSerializer):
    event_type_name = serializers.CharField(source="event_type.event_name", read_only=True, default=None)
    track_issued_time = serializers.DateTimeField(
        source="cyclone_track.issued_time", read_only=True, default=None
    )
    model_run_status = serializers.CharField(source="model_run.status", read_only=True)
    model_run_finished_at = serializers.DateTimeField(source="model_run.finished_at", read_only=True)
    model_run_created_at = serializers.DateTimeField(source="model_run.created_at", read_only=True)
    cyclone_name = serializers.CharField(source="cyclone_track.cyclone_name", read_only=True, default=None)
    is_forecast = serializers.SerializerMethodField()
    data_source = serializers.SerializerMethodField()
    forecast_runs = serializers.SerializerMethodField()

    def get_is_forecast(self, event):
        """REF events are exposure forecasts, not post-event assessments."""
        if event.model_run_id:
            return event.model_run.models.filter(name__iregex=r"rapid[ _-]?exposure").exists()
        return event.risks.filter(title__icontains="(REF)").exists()

    def get_data_source(self, event):
        return "rapid_exposure_forecast" if self.get_is_forecast(event) else "post_event_assessment"

    def get_forecast_runs(self, event):
        return forecast_runs_for_event(event)

    class Meta:
        model = Event
        fields = (
            "id",
            "title",
            "event_datetime",
            "event_type",
            "country",
            "cyclone_track",
            "hazards",
            "risks",
            "citizen_sciences",
            "model_run",
            "event_type_name",
            "cyclone_name",
            "track_issued_time",
            "model_run_status",
            "model_run_finished_at",
            "model_run_created_at",
            "is_forecast",
            "data_source",
            "forecast_runs",
        )
        read_only_fields = ("id",)
