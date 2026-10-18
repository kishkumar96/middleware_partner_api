"""Run the model queue from inside Django, with no HTTP round trip to the API itself.

runners/tc_runner.py is written against the Partner API's queue endpoints so a runner can
sit on another machine. Here the same job handling runs next to the database: DirectApi
answers the calls tc_runner makes (claim, heartbeat, upload, finish...) straight from the
ORM, so the claim leases, retries, supersession and publishing in model.queueing are the
ones the API uses.
"""

import functools
import importlib.util
import logging
import os
import shutil
import time
from pathlib import Path

from django.conf import settings
from django.core.files import File

from country.lookup import find_country
from cyclone_track.models import CycloneTrack
from risk_information.models import RiskInformation

from .models import ModelLogs, ModelRun, RunnerHeartbeat
from .queueing import claim_next_run, finish_run
from .views_queue import job_payload

log = logging.getLogger(__name__)

PUBLIC_API_URL = "https://pacific-disaster-api.spc.int/partner_api/v1"


@functools.lru_cache(maxsize=1)
def load_tc_runner():
    """runners/tc_runner.py, imported from its file (it is a script, not a package).

    Loaded once: its RunFailed class must be the same one the backend raises and the job
    handler catches, or a backend failure is reported as an unexpected error.
    """
    path = Path(settings.BASE_DIR) / "runners" / "tc_runner.py"
    spec = importlib.util.spec_from_file_location("tc_runner", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class DirectApi:
    """The slice of tc_runner.PartnerApi that job handling uses, answered from the database."""

    def __init__(self, url=None):
        # The address a model on the RiskScape platform fetches the track from; it must be reachable
        # from outside, so it is the public API's, not this process's own
        self.url = (url or os.environ.get("PARTNER_API_URL") or PUBLIC_API_URL).rstrip("/")

    def claim(self, server_name):
        run = claim_next_run(server_name)
        return None if run is None else job_payload(run, None)

    def heartbeat(self, server_name, backend):
        RunnerHeartbeat.objects.update_or_create(
            server_name=server_name,
            defaults={"backend": backend, "hostname": os.uname().nodename},
        )

    def finish(self, run_id, status, error="", external_url=""):
        run = ModelRun.objects.get(pk=run_id)
        event = finish_run(run, status, error=error[-10000:], external_url=external_url)
        return {"id": run.pk, "status": run.status, "event": event.pk if event else None}

    def log(self, run_id, text):
        try:
            ModelLogs.objects.create(model_run_id=run_id, logs=text)
        except Exception:
            log.exception("could not store a log line for run %s", run_id)

    def country_id(self, name):
        country = find_country(name)
        return country.pk if country else None

    def upload_output(self, job, path, country_id=None):
        runner = load_tc_runner()
        track = job["track"]
        run = ModelRun.objects.get(pk=job["id"])
        risk = RiskInformation(
            title=f"{track['storm_name']} {runner.issue_label(track)} {path.stem}",
            model_run=run,
            dataset_type=runner.dataset_type_for(path),
            country_id=country_id or track.get("country"),
        )
        with path.open("rb") as handle:
            risk.upload.save(path.name, File(handle), save=True)
        return {"id": risk.pk}

    def download(self, url, dest):
        """The track's own file, copied straight from storage."""
        track = CycloneTrack.objects.filter(track_file=self._storage_name(url)).first()
        if track is None:
            raise RuntimeError(f"no cyclone track file for {url}")
        with track.track_file.open("rb") as source, dest.open("wb") as target:
            shutil.copyfileobj(source, target)

    @staticmethod
    def _storage_name(url):
        media = settings.MEDIA_URL
        path = url.split("?")[0]
        if media in path:
            path = path.split(media, 1)[1]
        return path.lstrip("/")


def run_queue(*, server_name, backend_name, backend, work_dir, poll_seconds=60, once=False,
              validate_cmd=None, api=None, sleep=time.sleep):
    """Claim and handle queued runs for `server_name`; with `once`, at most one."""
    runner = load_tc_runner()
    api = api or DirectApi()
    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    while True:
        try:
            api.heartbeat(server_name, backend_name)
            job = api.claim(server_name)
        except Exception:
            log.exception("could not read the run queue")
            job = None
        if job:
            runner.handle(job, api, backend, work_dir.resolve(), validate_cmd)
            if once:
                return True
            continue
        if once:
            log.info("no queued runs for %s", server_name)
            return False
        sleep(poll_seconds)
