# Cyclone impact model runners

```
SPC posts a CycloneTrack ──► API queues a ModelRun per matching ModelTrigger
                               forecast track   → rapid model    (server_name riskscape.nz)
                               best track       → on-prem model  (server_name e.g. onprem-1)
                                       │
          tc_runner.py --backend platform        tc_runner.py --backend local
          (submits to the RiskScape platform)    (runs the RiskScape CLI on-prem)
                                       │
          outputs → POST risk_information (model_run=<id>) → POST model_run/<id>/finish/
                                       │
          API links the outputs to the storm's Event → the dashboard shows them
```

Runners only make outbound HTTPS calls to the Partner API, so the on-prem
server needs no inbound access.

## Setup

The quickest start is the setup command, which creates both pipelines:

```bash
python manage.py setup_tc_pipelines            # add --create-track-types on an empty database
```

- **rapid**: official forecast tracks -> the `Rapid-Exposure-Forecast-GeoJSON`
  model in `niwa/partner/rsmc-tcwc` on the RiskScape platform. Its `hazard`
  input is the track's `model_input` URL (see below), passed as a plain URL
  (`--rapid-project`, `--rapid-model`, `--rapid-param` to change).
- **pdna**: post-event best tracks -> QuickSurge -> Wind Hazard -> RiskScape
  (`Cyclone-PDIE`) on `lotgemdev01` (`--onprem-server` to change). The commands
  are the ones registered in production on 27 March 2026, without `-ti` (a
  runner has no terminal). Each docker step gets the track as the
  `CYCLONE_TRACK_URL` and `CYCLONE_TRACK_ID` environment variables; the
  RiskScape step must write its outputs to `{output_dir}`.

Re-running it updates the same rows. Everything it creates can then be
adjusted in the Django admin:

### Model chains

A model's **next model** runs after it in the same job, so one runner
executes the whole chain in order and stops at the first failing step. A
trigger points at the chain's first model. All models in a chain must have
the same `server_name`. Only files in `{output_dir}` after the last step are
uploaded, so the hazard steps can keep their intermediate files where they
are.

### Admin reference

1. **Cyclone track types**: the track types SPC posts must exist (e.g.
   "Official Forecast Track", "Post-event Best Track"). A type whose name
   contains "best" is treated as the authoritative post-event track: its
   results replace forecast results on the Event, never the other way round.
2. **Models**, one per thing a runner can execute:
   - Rapid: `name` = the RiskScape model id (e.g. `TC-Impact`),
     `server_name` = `riskscape.nz`, `location` = the project's full slug,
     `inputs` = model parameters, e.g.
     `{"track": "{track_path}", "cyclone_name": "'{storm}'", "issue_time": "'{issue_time}'"}`
     Templates can also use `{track_url}` (this track on the Partner API) and
     `{track_id}`, for models that fetch the track themselves.
     (text values are RiskScape expressions, so strings need single quotes).
   - On-prem: `server_name` = the name its runner uses, `location` = the
     directory to run in (the RiskScape project), `run_command` = the command
     template, e.g.
     `riskscape model run TC-Impact {params} --output {output_dir}`
     (check the flags against your RiskScape CLI version), and `inputs` as above.
3. **Model triggers**: which model to queue for which track type, optionally
   for one country only. Disable a trigger to pause automatic runs.

Tracks added through the admin don't queue runs on their own, so that
historical imports don't start model runs. Use the "Queue model runs for
selected tracks" action on cyclone tracks instead. Failed runs can be retried
with "Requeue selected runs" on model runs.

### What the runner does for a rapid (REF) run

- **Model-ready track.** The SPC serves track times as `2026-10-17 12:00:00`;
  the RiskScape models only parse `2026-10-17T12:00:00Z`, and on anything else
  they drop every point and still finish as complete with empty results.
  `GET /partner_api/v1/cyclone_track/<id>/model_input/` serves the same track
  with ISO-UTC times (the stored track is not changed), and `{model_track_url}`
  is that URL in a model's `inputs`. The model itself is not touched.
- **Empty-run check.** Before uploading, the runner fails a run whose own
  `cyclone-track` output has no track, so a model that couldn't read the track
  shows as a failed run, not as "no exposure". A real track that misses
  everything still passes.
- **Country from the results.** A track is filed under one country (SPC filed
  TC Wati under Fiji) but its exposure is for wherever the storm reaches. The
  runner reads `total-exposed-by-country.csv`, tags the outputs with the country
  with the most people exposed, and the Event is filed there. Other exposed
  countries are logged; one storm hitting two countries is filed under the
  larger only.
- **Forecast detection** matches the model's name as `rapid exposure` with a
  space, hyphen or underscore, so `Rapid-Exposure-Forecast-GeoJSON` counts.

## Running automatically, from inside Django

The queue is the database, so Django can work it itself. `docker-compose.yml` has a
`runner` service that runs:

```bash
python manage.py run_model_queue --server-name riskscape.nz     # loops; `--once` for cron
```

It claims runs the API queued when a track arrived, runs them on the RiskScape platform,
checks and uploads the outputs, and publishes them to the storm's Event, with no HTTP
round trip to the API itself. `restart: unless-stopped` keeps it alive across crashes and
reboots, and the queue's lease and retry rules (model/queueing.py) cover a worker that dies
mid-run. It needs `RISKSCAPE_TOKEN` (a RiskScape account that can run models in the model's
project; it does not need to write files there) and `PARTNER_API_URL` (the public address
the model fetches the track from) in `.env`. The on-prem post-event runner still uses
`tc_runner.py` below.

```bash
python manage.py check_runner_health            # exit 1 + a report; for cron or a monitor
python manage.py check_runner_health --email    # also email RUNNER_ALERT_EMAILS
```

reports a configured runner that has not checked in (default 5 min), runs waiting more than 30
minutes, and runs that failed in the last 6 hours.

## Running a script instead

Needs Python 3.10+ and `requests`.

```bash
export PARTNER_API_TOKEN=...   # a Partner API user token
export RISKSCAPE_TOKEN=...     # platform runner only
./tc_runner.py --server-name riskscape.nz --backend platform
./tc_runner.py --server-name onprem-1 --backend local
```

It polls every 60s by default; run it as a service, or from cron with
`--once`. `--validate-cmd "python3 check.py"` runs a check on the outputs
directory before anything is uploaded; a non-zero exit fails the run.

Each poll sends a runner heartbeat to the Partner API. Operators can inspect
`GET /partner_api/v1/model_run/operations/` to see the configured runners,
whether each runner has checked in during the last two minutes, queued work
for that runner, and run counts by status. The Django admin also shows runner
readiness on the Models and Runner heartbeats pages.

A run claimed by a runner that then goes quiet for 2 hours
(`MODEL_RUN_LEASE_SECONDS`) is requeued, up to 3 attempts
(`MODEL_RUN_MAX_ATTEMPTS`). A queued forecast run is skipped as superseded
when a newer advisory for the same storm arrives before it starts.

## Loading a REF run by hand

Until Rapid Exposure Forecast runs are fetched from the RiskScape platform
automatically, a run's downloaded outputs can be loaded as an event:

```bash
python manage.py import_ref_run /path/to/run-outputs --country VU \
    --title "TC Lola – Rapid exposure forecast" --event-time 2023-10-23T03:00:00Z
```

Files can keep the platform's run-name prefix (`test track-69-regional-summary.csv`).
Without `--track <id>`, a track is created from the run's `cyclone-track.geojson`.
`--replace` swaps out an earlier import with the same title. The dashboard shows
REF results as exposure (people, buildings and facilities under forecast winds),
not damage, because REF outputs have no loss columns.

## Output dataset types

Every newly uploaded output carries a `dataset_type` API field. The dashboard
uses this stable value before considering a filename, so storage paths and
run-name prefixes can change safely. Current types include `regional_impacts`,
`regional_summary`, `forecast_national`, `forecast_wind_swaths`,
`forecast_cyclone_track`, and `forecast_floodplain`. Legacy uploads without a
type retain filename-based compatibility. Unknown types are preserved on the
run/event for operators, but deliberately are not plotted until a UI renderer
exists for that kind of data.
