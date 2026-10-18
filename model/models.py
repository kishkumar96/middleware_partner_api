from django.db import models as dj_models
from django.utils import timezone


class ModelDomain(dj_models.Model):
    name = dj_models.CharField(max_length=255, unique=True)

    class Meta:
        db_table = "model_domain"

    def __str__(self) -> str:
        return self.name


class ModelPriority(dj_models.Model):
    name = dj_models.CharField(max_length=255, unique=True)

    class Meta:
        db_table = "model_priority"

    def __str__(self) -> str:
        return self.name


class ModelStatus(dj_models.Model):
    name = dj_models.CharField(max_length=255, unique=True)

    class Meta:
        db_table = "model_status"
        verbose_name = "Model status"
        verbose_name_plural = "Model status"

    def __str__(self) -> str:
        return self.name


class Model(dj_models.Model):
    name = dj_models.CharField(max_length=255)
    location = dj_models.CharField(max_length=500, blank=True)
    server_name = dj_models.CharField(max_length=255, blank=True)
    inputs = dj_models.JSONField(null=True, blank=True)
    dry_run_command = dj_models.TextField(blank=True)
    # Command a runner on server_name executes for a real run, as a template
    # (see runners/tc_runner.py). Unused by the RiskScape-platform runner,
    # which submits the run over the API instead.
    run_command = dj_models.TextField(blank=True)
    # Models can form a chain that runs as one job, in order: e.g. the
    # post-event chain QuickSurge -> Wind Hazard -> RiskScape, where the
    # hazard models must finish before the impact model reads their results.
    # A trigger points at the chain's first model. Every model in a chain
    # must be on the same server_name (one runner executes the whole chain).
    next_model = dj_models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=dj_models.SET_NULL,
        related_name="previous_models",
        help_text="Model to run after this one, in the same job",
    )

    class Meta:
        db_table = "model"

    def __str__(self) -> str:
        return self.name

    def chain(self) -> list["Model"]:
        """This model followed by each next_model, stopping at any cycle."""
        models, seen, current = [], set(), self
        while current is not None and current.pk not in seen:
            models.append(current)
            seen.add(current.pk)
            current = current.next_model
        return models


class ModelRun(dj_models.Model):
    name = dj_models.CharField(max_length=255, blank=True)
    models = dj_models.ManyToManyField(
        Model,
        related_name="runs",
        blank=True,
    )
    model_domain = dj_models.ForeignKey(
        ModelDomain,
        on_delete=dj_models.PROTECT,
        related_name="runs",
        db_column="model_domain_id",
    )
    tc_track = dj_models.ForeignKey(
        "cyclone_track.CycloneTrack",
        on_delete=dj_models.PROTECT,
        related_name="model_runs",
        db_column="tc_track_id",
    )
    priority = dj_models.ForeignKey(
        ModelPriority,
        on_delete=dj_models.PROTECT,
        related_name="runs",
    )

    model_run_completed = dj_models.BooleanField(default=False)

    # Queue state. A runner claims a QUEUED run for its server_name (see
    # model.queueing), and finishes it as COMPLETE or FAILED. model_run_completed
    # is kept in step with COMPLETE for existing consumers of that flag.
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETE = "complete"
    FAILED = "failed"
    SUPERSEDED = "superseded"
    STATUS_CHOICES = [
        (QUEUED, "Queued"),
        (RUNNING, "Running"),
        (COMPLETE, "Complete"),
        (FAILED, "Failed"),
        # A newer track for the same storm arrived before this run started
        (SUPERSEDED, "Superseded"),
    ]
    status = dj_models.CharField(max_length=20, choices=STATUS_CHOICES, default=QUEUED, db_index=True)
    attempts = dj_models.PositiveIntegerField(default=0)
    created_at = dj_models.DateTimeField(default=timezone.now)
    claimed_by = dj_models.CharField(max_length=255, blank=True)
    claimed_at = dj_models.DateTimeField(null=True, blank=True)
    finished_at = dj_models.DateTimeField(null=True, blank=True)
    # Where the run itself lives, e.g. the RiskScape platform model run
    external_url = dj_models.URLField(max_length=500, blank=True)
    error = dj_models.TextField(blank=True)

    class Meta:
        db_table = "model_run"
        ordering = ("-id",)

    def __str__(self) -> str:
        label = self.name or f"Run {self.pk}"
        return label


class ModelLogs(dj_models.Model):
    model_run = dj_models.ForeignKey(
        ModelRun,
        on_delete=dj_models.CASCADE,
        related_name="logs",
        db_column="model_run_id",
    )
    status = dj_models.ForeignKey(
        ModelStatus,
        null=True,
        blank=True,
        on_delete=dj_models.PROTECT,
        related_name="log_entries",
    )
    logs = dj_models.TextField()

    class Meta:
        db_table = "model_logs"
        verbose_name = "Model log"
        verbose_name_plural = "Model logs"

    def __str__(self) -> str:
        return f"Logs for run {self.model_run_id}"


class ModelTrigger(dj_models.Model):
    """Which model to queue when a cyclone track of a given type arrives.

    A trigger with no country matches tracks for any country; otherwise it
    only matches tracks for that country.
    """

    model = dj_models.ForeignKey(Model, on_delete=dj_models.CASCADE, related_name="triggers")
    cyclone_track_type = dj_models.ForeignKey(
        "cyclone_track_type.CycloneTrackType",
        on_delete=dj_models.CASCADE,
        related_name="model_triggers",
    )
    country = dj_models.ForeignKey(
        "country.Country",
        null=True,
        blank=True,
        on_delete=dj_models.CASCADE,
        related_name="model_triggers",
    )
    model_domain = dj_models.ForeignKey(ModelDomain, on_delete=dj_models.PROTECT, related_name="triggers")
    priority = dj_models.ForeignKey(ModelPriority, on_delete=dj_models.PROTECT, related_name="triggers")
    enabled = dj_models.BooleanField(default=True)

    class Meta:
        db_table = "model_trigger"

    def __str__(self) -> str:
        where = self.country.short_name if self.country_id else "any country"
        return f"{self.cyclone_track_type} ({where}) -> {self.model}"


class RunnerHeartbeat(dj_models.Model):
    """The last observed presence of a runner eligible to claim model jobs."""

    server_name = dj_models.CharField(max_length=255, unique=True)
    backend = dj_models.CharField(max_length=20, blank=True)
    hostname = dj_models.CharField(max_length=255, blank=True)
    last_seen_at = dj_models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "runner_heartbeat"
        ordering = ("server_name",)

    def __str__(self) -> str:
        return self.server_name
