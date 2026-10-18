"""Run queued cyclone impact models from inside Django.

    python manage.py run_model_queue                  # loops forever; for a service
    python manage.py run_model_queue --once           # handle at most one run; for cron

The queue is the database (see model.queueing): a new track posted to the API queues a run, and
this claims it, submits it to the RiskScape platform, checks and uploads the results, and
publishes them to the storm's Event. Run it as its own container (see docker-compose.yml) so a
web restart cannot lose a run in progress.

Environment: RISKSCAPE_TOKEN (a RiskScape account that can run the model's project),
RISKSCAPE_URL (default https://riskscape.nz), PARTNER_API_URL (the public address the model
fetches the track from; default the SPC's).
"""

import logging
import os
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from ...direct_runner import load_tc_runner, run_queue


class Command(BaseCommand):
    help = "Claim and run queued cyclone model runs, then publish their results."

    def add_arguments(self, parser):
        parser.add_argument("--server-name", default="riskscape.nz", help="claim runs whose Model.server_name is this")
        parser.add_argument("--work-dir", default="tc-runner-work")
        parser.add_argument("--poll-seconds", type=int, default=60, help="wait between empty queue checks")
        parser.add_argument("--once", action="store_true", help="handle at most one run, then exit")
        parser.add_argument("--validate-cmd", help="run as: <cmd> <outputs-dir>; non-zero exit rejects a run")

    def make_backend(self):
        token = os.environ.get("RISKSCAPE_TOKEN")
        if not token:
            raise CommandError("RISKSCAPE_TOKEN is not set")
        runner = load_tc_runner()
        return runner.PlatformBackend(os.environ.get("RISKSCAPE_URL", "https://riskscape.nz"), token)

    def handle(self, *args, **options):
        logging.getLogger().setLevel(logging.INFO)
        run_queue(
            server_name=options["server_name"],
            backend_name="platform",
            backend=self.make_backend(),
            work_dir=Path(options["work_dir"]),
            poll_seconds=options["poll_seconds"],
            once=options["once"],
            validate_cmd=options["validate_cmd"],
        )
