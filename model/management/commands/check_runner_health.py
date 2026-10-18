"""Say whether the model queue is being worked, and exit non-zero if it is not.

    python manage.py check_runner_health                 # for cron or a monitor
    python manage.py check_runner_health --email         # also email RUNNER_ALERT_EMAILS

A live cyclone must not fail quietly. It reports a configured runner that has not checked in,
runs that have waited too long to be picked up, and runs that failed recently.
"""

import datetime

from django.conf import settings
from django.core.mail import send_mail
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from ...models import Model, ModelRun, RunnerHeartbeat


def runner_problems(now, max_silence, max_queued, failed_within):
    problems = []
    heartbeats = {row.server_name: row for row in RunnerHeartbeat.objects.all()}
    servers = sorted(set(Model.objects.exclude(server_name="").values_list("server_name", flat=True)))
    for server in servers:
        beat = heartbeats.get(server)
        if beat is None:
            problems.append(f"{server}: has never checked in")
        elif now - beat.last_seen_at > max_silence:
            minutes = int((now - beat.last_seen_at).total_seconds() // 60)
            problems.append(f"{server}: no heartbeat for {minutes} min")
    waiting = ModelRun.objects.filter(status=ModelRun.QUEUED, created_at__lt=now - max_queued).distinct()
    for run in waiting:
        minutes = int((now - run.created_at).total_seconds() // 60)
        problems.append(f"run {run.pk} ({run.name}) has been queued for {minutes} min")
    failed = ModelRun.objects.filter(status=ModelRun.FAILED, finished_at__gte=now - failed_within)
    for run in failed:
        problems.append(f"run {run.pk} ({run.name}) failed: {(run.error or 'no reason recorded')[:200]}")
    return problems


class Command(BaseCommand):
    help = "Report a silent runner, runs stuck in the queue, and recent failures; exit 1 if any."

    def add_arguments(self, parser):
        parser.add_argument("--max-silence-minutes", type=int, default=5)
        parser.add_argument("--max-queued-minutes", type=int, default=30)
        parser.add_argument("--failed-within-hours", type=int, default=6)
        parser.add_argument("--email", action="store_true", help="email RUNNER_ALERT_EMAILS when there is a problem")

    def handle(self, *args, **options):
        problems = runner_problems(
            timezone.now(),
            datetime.timedelta(minutes=options["max_silence_minutes"]),
            datetime.timedelta(minutes=options["max_queued_minutes"]),
            datetime.timedelta(hours=options["failed_within_hours"]),
        )
        if not problems:
            self.stdout.write(self.style.SUCCESS("Model queue healthy."))
            return
        report = "\n".join(f"- {line}" for line in problems)
        if options["email"]:
            recipients = [a.strip() for a in getattr(settings, "RUNNER_ALERT_EMAILS", "").split(",") if a.strip()]
            if recipients:
                send_mail("Cyclone model queue needs attention", report, None, recipients, fail_silently=True)
        raise CommandError(f"Model queue needs attention:\n{report}")
