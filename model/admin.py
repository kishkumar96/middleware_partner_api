from datetime import timedelta

from django.contrib import admin

from django.utils import timezone

from .models import (
    Model,
    ModelDomain,
    ModelLogs,
    ModelPriority,
    ModelRun,
    ModelStatus,
    ModelTrigger,
    RunnerHeartbeat,
)


@admin.register(ModelDomain)
class ModelDomainAdmin(admin.ModelAdmin):
    list_display = ("name",)
    search_fields = ("name",)


@admin.register(ModelPriority)
class ModelPriorityAdmin(admin.ModelAdmin):
    list_display = ("name",)
    search_fields = ("name",)


@admin.register(ModelStatus)
class ModelStatusAdmin(admin.ModelAdmin):
    list_display = ("name",)
    search_fields = ("name",)


@admin.register(Model)
class ModelAdmin(admin.ModelAdmin):
    list_display = ("name", "server_name", "next_model", "runner_readiness", "location")
    search_fields = ("name", "server_name", "location")

    @admin.display(description="Runner")
    def runner_readiness(self, obj):
        heartbeat = RunnerHeartbeat.objects.filter(server_name=obj.server_name).first()
        if not heartbeat or heartbeat.last_seen_at < timezone.now() - timedelta(minutes=2):
            return "Offline — no heartbeat in 2 min"
        return f"Online ({heartbeat.hostname or heartbeat.backend})"


@admin.register(ModelRun)
class ModelRunAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "status", "attempts", "claimed_by", "tc_track", "created_at", "finished_at", "failure_summary")
    list_filter = ("status", "model_domain", "priority")
    search_fields = ("name", "models__name")
    list_select_related = ("model_domain", "tc_track", "priority")
    filter_horizontal = ("models",)
    readonly_fields = ("claimed_by", "claimed_at", "finished_at", "attempts")
    actions = ("requeue",)

    @admin.display(description="Failure")
    def failure_summary(self, obj):
        return obj.error[:120] if obj.error else "—"

    @admin.action(description="Requeue selected runs")
    def requeue(self, request, queryset):
        count = queryset.update(
            status=ModelRun.QUEUED,
            model_run_completed=False,
            attempts=0,
            claimed_by="",
            claimed_at=None,
            finished_at=None,
            error="",
            created_at=timezone.now(),
        )
        self.message_user(request, f"Requeued {count} run(s).")


@admin.register(ModelTrigger)
class ModelTriggerAdmin(admin.ModelAdmin):
    list_display = ("cyclone_track_type", "country", "model", "model_domain", "priority", "enabled")
    list_filter = ("enabled", "cyclone_track_type", "country")
    list_select_related = ("cyclone_track_type", "country", "model", "model_domain", "priority")


@admin.register(ModelLogs)
class ModelLogsAdmin(admin.ModelAdmin):
    list_display = ("id", "model_run", "status")
    search_fields = ("model_run__name", "model_run__id")
    list_select_related = ("model_run", "status")


@admin.register(RunnerHeartbeat)
class RunnerHeartbeatAdmin(admin.ModelAdmin):
    list_display = ("server_name", "backend", "hostname", "last_seen_at", "is_online")
    search_fields = ("server_name", "hostname")
    readonly_fields = ("last_seen_at",)

    @admin.display(boolean=True, description="Online")
    def is_online(self, obj):
        return obj.last_seen_at >= timezone.now() - timedelta(minutes=2)

