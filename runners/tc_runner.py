#!/usr/bin/env python3
"""Run queued cyclone impact models and send the results back to the Partner API.

Polls the Partner API's model run queue for jobs whose model lives on this
server (Model.server_name), runs each one, uploads its outputs as
risk_information rows tagged with the run, and reports the result. The API
then links the outputs to the storm's Event, which the dashboard reads.

Two backends:
  platform  Submit the run to the RiskScape platform over its API (rapid
            forecast runs). Model.location is the project's full slug and
            Model.name its model id.
  local     Run a command on this machine, e.g. the RiskScape CLI (post-event
            best track runs). Model.run_command is the command template.

Model.inputs maps model parameter names to templates, e.g.
  {"track": "{track_path}", "cyclone_name": "'{storm}'", "issue_time": "'{issue_time}'"}
Templates can use: {storm} {issue_time} {track_id} {track_url} {model_track_url} {run_id}
{country} {model} {location} {output_dir} {track_csv} {track_geojson}
{track_path} (track_path is the uploaded file on the platform, or
track_geojson locally; track_url is this track on the Partner API, for models
that fetch the track themselves; model_track_url is the same track with its times
in the ISO-UTC form the RiskScape models parse, which the SPC's own are not).
In run_command, a lone {params} token expands to `-p name=value` for each input.

A job can be a chain of models (Model.next_model), e.g. the post-event
QuickSurge -> Wind Hazard -> RiskScape: they run in order on this runner, the
chain stops at the first failure, and the files in {output_dir} after the
last step are what gets uploaded.

Environment:
  PARTNER_API_URL    default https://pacific-disaster-api.spc.int/partner_api/v1
  PARTNER_API_TOKEN  Partner API token (required)
  RISKSCAPE_URL      default https://riskscape.nz (platform backend)
  RISKSCAPE_TOKEN    RiskScape API token (platform backend)

Example:
  ./tc_runner.py --server-name riskscape.nz --backend platform
  ./tc_runner.py --server-name onprem-1 --backend local --once
"""

import argparse
import csv
import datetime
import json
import logging
import os
import shlex
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import quote

import requests


DATASET_TYPE_BY_FILENAME = {
    "regional-impacts.geojson": "regional_impacts",
    "regional-summary.csv": "regional_summary",
    "total-exposed-by-country.csv": "forecast_national",
    "total-by-windspeed.csv": "forecast_by_wind_speed",
    "total-by-track-distance.csv": "forecast_by_track_distance",
    "exposure-by-cluster.geojson": "exposure_by_cluster",
    "cyclone-track.geojson": "forecast_cyclone_track",
    "wind-swaths.geojson": "forecast_wind_swaths",
    "track-distance.geojson": "forecast_track_distance",
    "floodplain-exposure.geojson": "forecast_floodplain",
    "damaged-buildings.geojson": "damaged_buildings",
    "damaged-roads.geojson": "damaged_roads",
}


def dataset_type_for(path):
    """Stable API metadata for known outputs; unknown files stay available
    to operators without pretending that the dashboard can plot them."""
    return DATASET_TYPE_BY_FILENAME.get(path.name.lower(), "")

log = logging.getLogger("tc_runner")

# Country names the models write, as codes. Mirrors country/codes.py (this script runs without Django);
# a test keeps the two in step.
COUNTRY_NAME_CODES = {
    "fiji": "FJ",
    "vanuatu": "VU",
    "cook islands": "CK",
    "cook island": "CK",
    "tonga": "TO",
    "samoa": "WS",
}

OUTPUT_SUFFIXES = {".gpkg", ".csv", ".geojson"}
MEDIA_SUFFIXES = {
    "text/csv": ".csv",
    "application/geopackage+sqlite3": ".gpkg",
    "application/geo+json": ".geojson",
}


class RunFailed(Exception):
    pass


class PartnerApi:
    def __init__(self, url, token):
        self.url = url.rstrip("/")
        self.session = requests.Session()
        self.session.headers["Authorization"] = f"Token {token}"
        self.session.headers["Accept"] = "application/json"

    def _post(self, path, **kwargs):
        response = self.session.post(f"{self.url}/{path}", timeout=120, **kwargs)
        if not response.ok:
            raise RuntimeError(f"POST {path}: HTTP {response.status_code}: {response.text[:2000]}")
        return response

    def claim(self, server_name):
        response = self._post("model_run/claim/", json={"server_name": server_name})
        return None if response.status_code == 204 else response.json()

    def heartbeat(self, server_name, backend):
        return self._post(
            "model_run/heartbeat/",
            json={"server_name": server_name, "backend": backend, "hostname": os.uname().nodename},
        ).json()

    def finish(self, run_id, status, error="", external_url=""):
        body = {"status": status, "error": error[-10000:], "external_url": external_url}
        return self._post(f"model_run/{run_id}/finish/", json=body).json()

    def log(self, run_id, text):
        try:
            self._post("model_run_logs/", json={"model_run": run_id, "logs": text})
        except Exception:
            log.exception("could not send log line for run %s", run_id)

    def country_id(self, name):
        """The Partner API's id for a country given by code ("CK") or name, or None.

        The country's canonical `code` first; then a name the codes table knows (RiskScape writes
        "Cook Islands", the live row says "Cook Island"); then the row's own names.
        """
        if not hasattr(self, "_countries"):
            response = self.session.get(f"{self.url}/country/", timeout=60)
            response.raise_for_status()
            data = response.json()
            self._countries = data["results"] if isinstance(data, dict) else data
        text = name.strip()

        def with_code(code):
            for country in self._countries:
                if str(country.get("code") or "").strip().upper() == code:
                    return country["id"]
            return None

        found = with_code(text.upper())
        if found is None and text.casefold() in COUNTRY_NAME_CODES:
            found = with_code(COUNTRY_NAME_CODES[text.casefold()])
        if found is not None:
            return found
        wanted = text.casefold()
        for country in self._countries:
            if wanted in (str(country.get("long_name", "")).casefold(), str(country.get("short_name", "")).casefold()):
                return country["id"]
        return None

    def upload_output(self, job, path, country_id=None):
        track = job["track"]
        title = f"{track['storm_name']} {issue_label(track)} {path.stem}"
        with path.open("rb") as f:
            data = {
                "title": title,
                "model_run": job["id"],
                "dataset_type": dataset_type_for(path),
            }
            country = country_id or track.get("country")
            if country:
                data["country"] = country
            return self._post("risk_information/", data=data, files={"upload": (path.name, f)}).json()

    def download(self, url, dest):
        with self.session.get(url, stream=True, timeout=120) as response:
            response.raise_for_status()
            with dest.open("wb") as f:
                shutil.copyfileobj(response.raw, f)


def issue_label(track):
    issued = track.get("issued_time")
    if not issued:
        return f"track{track['id']}"
    return datetime.datetime.fromisoformat(issued.replace("Z", "+00:00")).strftime("%Y%m%dT%H%MZ")


def check_track_output(output_dir):
    """Fail a run whose own track output holds no track.

    When a model cannot read the track it drops every point, finishes as
    complete, and writes empty tables that look like "no exposure". A real
    track that misses everything still has its line in this output.
    """
    for path in sorted(output_dir.rglob("cyclone-track.*")):
        suffix = path.suffix.lower()
        if suffix == ".geojson":
            data = json.loads(path.read_text(encoding="utf-8-sig"))
            empty = not any((f.get("geometry") or {}).get("coordinates") for f in data.get("features") or [])
        elif suffix == ".gpkg":
            with sqlite3.connect(path) as db:
                tables = [row[0] for row in db.execute("SELECT table_name FROM gpkg_contents")]
                empty = all(db.execute(f'SELECT count(*) FROM "{t}"').fetchone()[0] == 0 for t in tables)
        else:
            continue
        if empty:
            raise RunFailed(
                f"{path.name} has no track: the model dropped every track point, so its empty "
                "tables are a failed run, not zero exposure. Check the run's warnings."
            )


def results_country(output_dir):
    """(country name, other exposed countries) from the run's national table, or (None, []).

    The country with the most people exposed. A track is filed under one country, but
    the exposure it produces is for wherever the storm reaches.
    """
    for path in sorted(output_dir.rglob("total-exposed-by-country.csv")):
        with path.open(encoding="utf-8-sig", newline="") as f:
            rows = []
            for row in csv.DictReader(f):
                name = (row.get("Country") or row.get("Region") or "").strip()
                try:
                    people = float(row.get("Exposed_Population") or 0)
                except ValueError:
                    people = 0.0
                if name and people > 0:
                    rows.append((people, name))
        rows.sort(reverse=True)
        if rows:
            return rows[0][1], [name for _, name in rows[1:]]
    return None, []


def fill(template, variables):
    try:
        return template.format_map(variables)
    except KeyError as e:
        raise RunFailed(f"unknown template variable {e} in {template!r}") from None


class PlatformBackend:
    """Submit the run to the RiskScape platform and download its outputs."""

    def __init__(self, url, token, poll_seconds=10, timeout_seconds=3600):
        self.url = url.rstrip("/")
        self.poll_seconds = poll_seconds
        self.timeout_seconds = timeout_seconds
        self.session = requests.Session()
        self.session.auth = ("TOKEN", token)
        self.session.headers["Accept"] = "application/json"

    def _call(self, method, path_or_url, **kwargs):
        url = path_or_url if path_or_url.startswith("http") else f"{self.url}{path_or_url}"
        response = self.session.request(method, url, timeout=kwargs.pop("timeout", 120), **kwargs)
        if not response.ok:
            raise RunFailed(f"RiskScape {method} {url}: HTTP {response.status_code}: {response.text[:2000]}")
        return response

    def run(self, job, model, variables, output_dir):
        project = f"/api/projects/by-full-slug/{model['location']}"

        # Only a model that reads the track from a project file ({track_path}) needs it uploaded.
        # One that fetches it from a URL ({model_track_url}) needs no write access to the project,
        # which a runner's account may not have (rsmc-tcwc refuses uploads from it).
        if any("{track_path}" in str(template) for template in (model["inputs"] or {}).values()):
            track_path = f"Cyclone-Tracks/{variables['storm']}/{variables['issue_time']}.geojson"
            self._call(
                "PUT",
                f"{project}/files/by-path/{quote(track_path)}",
                data=Path(variables["track_geojson"]).read_bytes(),
                headers={"Content-Type": "application/geo+json"},
            )
            variables["track_path"] = track_path

        request = {
            "externalModelId": model["name"],
            "managed": True,
            "parameters": {name: {"value": fill(t, variables)} for name, t in (model["inputs"] or {}).items()},
        }
        run = self._call("POST", f"{project}/runs/new", json=request, timeout=300).json()
        run_url = f"{self.url}{run['uri']}"
        log.info("RiskScape run %s started: %s", run["id"], run_url)

        deadline = time.monotonic() + self.timeout_seconds
        while not run.get("finished"):
            if time.monotonic() > deadline:
                raise RunFailed(f"RiskScape run {run['id']} not finished after {self.timeout_seconds}s", run_url)
            time.sleep(self.poll_seconds)
            run = self._call("GET", run_url).json()
        if run["state"] != "COMPLETE":
            detail = json.dumps({k: run.get(k) for k in ("state", "failureDetail", "problems")}, indent=1)
            raise RunFailed(f"RiskScape run {run['id']} ended {run['state']}:\n{detail}", run_url)

        for output in run.get("outputs", []):
            suffix = MEDIA_SUFFIXES.get(output.get("mediaType"), ".bin")
            dest = output_dir / f"{output['name']}{suffix}"
            response = self._call("GET", f"{self.url}{output['uri']}/download", stream=True, timeout=600)
            with dest.open("wb") as f:
                shutil.copyfileobj(response.raw, f)

        # Platform runs are deleted after a week unless named. The results are
        # copied to the Partner API anyway, so a failure here isn't fatal.
        # UNVERIFIED: endpoint and body format; check against the RiskScape API docs.
        try:
            label = f"{variables['storm']} {variables['issue_time']} (Partner run {job['id']})"
            self._call("PUT", f"{run_url}/name", data=label.encode(), headers={"Content-Type": "text/plain"})
        except RunFailed as e:
            log.warning("could not name RiskScape run: %s", e)
        return run_url


class LocalBackend:
    """Run Model.run_command on this machine, e.g. the RiskScape CLI."""

    def __init__(self, timeout_seconds=6 * 3600):
        self.timeout_seconds = timeout_seconds

    def run(self, job, model, variables, output_dir):
        if not model.get("run_command"):
            raise RunFailed(f"model {model['name']!r} has no run_command")

        variables["track_path"] = variables["track_geojson"]
        # Split the template before filling it in, so values with spaces or
        # quotes stay one argument and nothing goes through a shell.
        command = []
        for token in shlex.split(model["run_command"]):
            if token == "{params}":
                for name, template in (model["inputs"] or {}).items():
                    command += ["-p", f"{name}={fill(template, variables)}"]
            else:
                command.append(fill(token, variables))

        log.info("running: %s", shlex.join(command))
        try:
            result = subprocess.run(
                command,
                cwd=model.get("location") or None,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
            )
        except subprocess.TimeoutExpired:
            raise RunFailed(f"command timed out after {self.timeout_seconds}s") from None
        (output_dir.parent / "command.log").write_text(result.stdout + result.stderr)
        if result.returncode != 0:
            raise RunFailed(f"command exited {result.returncode}:\n{(result.stdout + result.stderr)[-5000:]}")
        return ""


def process(job, api, backend, work_dir, validate_cmd):
    track = job["track"]
    run_dir = work_dir / f"run-{job['id']}"
    shutil.rmtree(run_dir, ignore_errors=True)
    output_dir = run_dir / "outputs"
    output_dir.mkdir(parents=True)

    if not track.get("geometry"):
        raise RunFailed(f"cyclone track {track['id']} has no computed geometry")
    track_geojson = run_dir / "track.geojson"
    track_geojson.write_text(json.dumps(track["geometry"]))
    track_csv = run_dir / "track.csv"
    api.download(track["track_file"], track_csv)

    variables = {
        "storm": track["storm_name"],
        "issue_time": issue_label(track),
        "track_id": track["id"],
        "track_url": f"{api.url}/cyclone_track/{track['id']}/?format=json",
        "model_track_url": f"{api.url}/cyclone_track/{track['id']}/model_input/",
        "run_id": job["id"],
        "country": track.get("country_code") or "",
        "output_dir": str(output_dir),
        "track_csv": str(track_csv),
        "track_geojson": str(track_geojson),
    }

    models = job.get("models") or [job["model"]]
    external_url = ""
    for step, model in enumerate(models, start=1):
        label = f"Step {step}/{len(models)}: {model['name']}"
        log.info("run %s %s", job["id"], label)
        if len(models) > 1:
            api.log(job["id"], f"{label} started")
        step_variables = {**variables, "model": model["name"], "location": model.get("location") or ""}
        try:
            external_url = backend.run(job, model, step_variables, output_dir) or external_url
        except RunFailed as e:
            raise RunFailed(f"{label} failed: {e.args[0]}", *e.args[1:]) from None

    outputs = sorted(p for p in output_dir.rglob("*") if p.suffix.lower() in OUTPUT_SUFFIXES)
    if not outputs:
        raise RunFailed(f"no {'/'.join(sorted(OUTPUT_SUFFIXES))} outputs in {output_dir}", external_url)
    for path in outputs:
        if path.stat().st_size == 0:
            raise RunFailed(f"output {path.name} is empty", external_url)
    try:
        check_track_output(output_dir)
    except RunFailed as e:
        raise RunFailed(e.args[0], external_url) from None
    if validate_cmd:
        result = subprocess.run([*shlex.split(validate_cmd), str(output_dir)], capture_output=True, text=True)
        if result.returncode != 0:
            raise RunFailed(f"validation rejected outputs:\n{(result.stdout + result.stderr)[-5000:]}", external_url)

    country_name, other_countries = results_country(output_dir)
    country_id = api.country_id(country_name) if country_name else None
    if other_countries:
        log.warning("run %s also exposes %s; filed under %s only", job["id"], ", ".join(other_countries), country_name)
    if country_name and country_id is None:
        log.warning("run %s: no country called %r, using the track's", job["id"], country_name)
    for path in outputs:
        row = api.upload_output(job, path, country_id)
        log.info("uploaded %s as risk_information %s", path.name, row["id"])
    return external_url, run_dir


def handle(job, api, backend, work_dir, validate_cmd):
    run_id = job["id"]
    track = job["track"]
    log.info("claimed run %s: %s %s (attempt %s)", run_id, track["storm_name"], issue_label(track), job["attempts"])
    api.log(run_id, f"Claimed by {os.uname().nodename}, attempt {job['attempts']}")
    try:
        external_url, run_dir = process(job, api, backend, work_dir, validate_cmd)
    except Exception as e:
        external_url = e.args[1] if isinstance(e, RunFailed) and len(e.args) > 1 else ""
        message = e.args[0] if isinstance(e, RunFailed) else f"{type(e).__name__}: {e}"
        log.error("run %s failed: %s", run_id, message)
        api.log(run_id, f"Failed: {message}")
        api.finish(run_id, "failed", error=message, external_url=external_url)
        return
    result = api.finish(run_id, "complete", external_url=external_url)
    api.log(run_id, f"Complete; published to event {result.get('event')}")
    log.info("run %s complete, event %s", run_id, result.get("event"))
    shutil.rmtree(run_dir, ignore_errors=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--server-name", required=True, help="claim runs whose Model.server_name is this")
    parser.add_argument("--backend", choices=("platform", "local"), required=True)
    parser.add_argument("--work-dir", type=Path, default=Path("tc-runner-work"))
    parser.add_argument("--poll-seconds", type=int, default=60, help="wait between empty queue checks")
    parser.add_argument("--once", action="store_true", help="handle at most one job, then exit")
    parser.add_argument("--validate-cmd", help="run as: <cmd> <outputs-dir>; non-zero exit rejects the run")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    api_token = os.environ.get("PARTNER_API_TOKEN")
    if not api_token:
        sys.exit("PARTNER_API_TOKEN is not set")
    api = PartnerApi(os.environ.get("PARTNER_API_URL", "https://pacific-disaster-api.spc.int/partner_api/v1"), api_token)

    if args.backend == "platform":
        rs_token = os.environ.get("RISKSCAPE_TOKEN")
        if not rs_token:
            sys.exit("RISKSCAPE_TOKEN is not set")
        backend = PlatformBackend(os.environ.get("RISKSCAPE_URL", "https://riskscape.nz"), rs_token)
    else:
        backend = LocalBackend()

    args.work_dir.mkdir(parents=True, exist_ok=True)
    while True:
        try:
            api.heartbeat(args.server_name, args.backend)
            job = api.claim(args.server_name)
        except Exception:
            log.exception("could not reach the Partner API")
            job = None
        if job:
            handle(job, api, backend, args.work_dir.resolve(), args.validate_cmd)
            if args.once:
                return
            continue
        if args.once:
            log.info("no queued runs for %s", args.server_name)
            return
        time.sleep(args.poll_seconds)


if __name__ == "__main__":
    main()
