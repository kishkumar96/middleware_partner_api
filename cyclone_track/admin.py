from django import forms
from django.contrib import admin
from django.contrib import messages

from .models import CycloneTrack
from mailer.utils import SPCMailer
from model.queueing import queue_runs_for_track
from model.run_descriptions import describe_queued_run, explain_nothing_queued


class CycloneTrackAdminForm(forms.ModelForm):
    start_runs = forms.BooleanField(
        required=False,
        label="Start model runs when saved",
        help_text=(
            "Queue the pipeline for this track type (forecast: Rapid, best track: PDNA) as soon as "
            "the track is saved. Leave unticked when importing historical storms."
        ),
    )

    class Meta:
        model = CycloneTrack
        fields = "__all__"


@admin.register(CycloneTrack)
class CycloneTrackAdmin(admin.ModelAdmin):
    form = CycloneTrackAdminForm
    list_display = (
        "cyclone_name",
        "issued_time",
        "issued_agency",
        "cyclone_track_type",
        "track_file",
        "geometry_computed",
        "country",
        "notify",
        "mail_configuration",
    )
    list_select_related = ("country", "cyclone_track_type")
    readonly_fields = ("geometry",)
    search_fields = (
        "cyclone_name",
        "issued_agency",
        "track_file",
        "country__short_name",
        "country__long_name",
    )

    actions = ("queue_model_runs",)

    @admin.action(description="Queue model runs for selected tracks")
    def queue_model_runs(self, request, queryset):
        # Tracks added in the admin don't queue runs on their own (unless
        # "Start model runs when saved" is ticked), so that importing
        # historical storms doesn't start model runs.
        for track in queryset.select_related("cyclone_track_type", "country"):
            self.start_runs(request, track)

    def start_runs(self, request, track):
        """Queue the track's pipeline and say what happened, or why nothing did."""
        if not track.geometry_computed:
            self.message_user(request, explain_nothing_queued(track), level=messages.WARNING)
            return
        runs = queue_runs_for_track(track)
        for run in runs:
            self.message_user(request, describe_queued_run(run), level=messages.SUCCESS)
        if not runs:
            self.message_user(request, explain_nothing_queued(track), level=messages.WARNING)

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)

        if form.cleaned_data.get("start_runs"):
            obj.refresh_from_db()  # geometry is computed during save
            self.start_runs(request, obj)

        # Only send on initial create in admin.
        if change:
            return
        if not obj.notify:
            return
        if not obj.mail_configuration_id:
            self.message_user(
                request,
                "notify=true but no mail_configuration was selected; email not sent.",
                level=messages.ERROR,
            )
            return

        result = SPCMailer.send_with_config_sync(obj.mail_configuration)
        if not result.ok:
            self.message_user(
                request,
                f"Email send failed (Graph {result.status_code}): {result.response_text}",
                level=messages.ERROR,
            )
            return

        self.message_user(request, "Notification email sent.", level=messages.SUCCESS)
