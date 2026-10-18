import datetime
import io
import json

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import CommandError, call_command
from django.utils import timezone
from rest_framework import status
from django.test import SimpleTestCase
from rest_framework.test import APITestCase

from country.models import Country
from cyclone_track.models import CycloneTrack
from cyclone_track_type.models import CycloneTrackType
from event.models import Event
from event_type.models import EventType
from risk_information.models import RiskInformation

from .models import Model, ModelDomain, ModelPriority, ModelRun, ModelTrigger, RunnerHeartbeat
from .queueing import (
    LEASE_SECONDS,
    MAX_ATTEMPTS,
    claim_next_run,
    finish_run,
    ordered_run_models,
    queue_runs_for_track,
    storm_name,
)

GEOJSON = json.dumps(
    {
        "type": "FeatureCollection",
        "features": [{"type": "Feature", "properties": {}, "geometry": {"type": "Point", "coordinates": [0, 0]}}],
    }
).encode("utf-8")


def utc(day, hour=0):
    return datetime.datetime(2026, 4, day, hour, tzinfo=datetime.timezone.utc)


class QueueTestCase(APITestCase):
    def setUp(self):
        self.vu = Country.objects.create(short_name="VU", long_name="Vanuatu")
        self.to = Country.objects.create(short_name="TO", long_name="Tonga")
        self.forecast = CycloneTrackType.objects.create(name="Official Forecast Track")
        self.best = CycloneTrackType.objects.create(name="Post-event Best Track")
        self.domain = ModelDomain.objects.create(name="Tropical cyclone")
        self.priority = ModelPriority.objects.create(name="High")
        self.rapid = Model.objects.create(name="TC-Impact", server_name="riskscape.nz", location="niwa/partner/tc")
        self.onprem = Model.objects.create(name="TC-Impact-Detailed", server_name="onprem-1")
        self.trigger(self.forecast, self.rapid)
        self.trigger(self.best, self.onprem)

    def trigger(self, track_type, model, country=None):
        return ModelTrigger.objects.create(
            cyclone_track_type=track_type,
            model=model,
            country=country,
            model_domain=self.domain,
            priority=self.priority,
        )

    def track(self, issued, track_type=None, country=None, name=None):
        track_type = track_type or self.forecast
        return CycloneTrack.objects.create(
            cyclone_name=name or f"{issued:%Y%m%d%H}_VAIANU",
            issued_time=issued,
            cyclone_track_type=track_type,
            country=country or self.vu,
            track_file=SimpleUploadedFile("track.geojson", GEOJSON),
        )

    def risk(self, run, title="impact", country=None):
        # A run's results are normally for the country its track belongs to
        return RiskInformation.objects.create(
            title=title,
            country=country or run.tc_track.country or self.vu,
            model_run=run,
            upload=SimpleUploadedFile(f"{title}.geojson", GEOJSON),
        )

    def run_to_completion(self, track):
        """Queue, claim and complete one run for a track, with one output."""
        (run,) = queue_runs_for_track(track)
        server = run.models.get().server_name
        self.assertEqual(claim_next_run(server).pk, run.pk)
        risk = self.risk(run)
        event = finish_run(run, ModelRun.COMPLETE)
        return run, risk, event


class StormNameTests(QueueTestCase):
    def test_strips_spc_issue_prefix(self):
        self.assertEqual(storm_name("2026040812_VAIANU"), "VAIANU")
        self.assertEqual(storm_name("Harold"), "Harold")
        self.assertEqual(storm_name(None), "")


class QueueRunsTests(QueueTestCase):
    def test_forecast_track_queues_rapid_model(self):
        runs = queue_runs_for_track(self.track(utc(8, 12)))
        self.assertEqual(len(runs), 1)
        self.assertEqual(list(runs[0].models.all()), [self.rapid])
        self.assertEqual(runs[0].status, ModelRun.QUEUED)

    def test_best_track_queues_onprem_model(self):
        (run,) = queue_runs_for_track(self.track(utc(22), track_type=self.best))
        self.assertEqual(list(run.models.all()), [self.onprem])

    def test_untriggered_type_and_disabled_trigger_queue_nothing(self):
        analysis = CycloneTrackType.objects.create(name="Analysis Track")
        self.assertEqual(queue_runs_for_track(self.track(utc(1), track_type=analysis)), [])
        ModelTrigger.objects.update(enabled=False)
        self.assertEqual(queue_runs_for_track(self.track(utc(2))), [])

    def test_country_specific_trigger_only_matches_its_country(self):
        to_model = Model.objects.create(name="TO-Impact", server_name="riskscape.nz")
        self.trigger(self.forecast, to_model, country=self.to)
        vu_runs = queue_runs_for_track(self.track(utc(3), country=self.vu))
        to_runs = queue_runs_for_track(self.track(utc(3), country=self.to))
        self.assertEqual([r.models.get() for r in vu_runs], [self.rapid])
        self.assertCountEqual([r.models.get() for r in to_runs], [self.rapid, to_model])

    def test_queueing_twice_does_not_duplicate(self):
        track = self.track(utc(4))
        queue_runs_for_track(track)
        self.assertEqual(queue_runs_for_track(track), [])
        self.assertEqual(ModelRun.objects.count(), 1)

    def test_newer_advisory_supersedes_queued_older_one(self):
        (old,) = queue_runs_for_track(self.track(utc(5, 0)))
        (new,) = queue_runs_for_track(self.track(utc(5, 6)))
        old.refresh_from_db()
        self.assertEqual(old.status, ModelRun.SUPERSEDED)
        self.assertEqual(new.status, ModelRun.QUEUED)

    def test_running_and_other_storm_runs_are_not_superseded(self):
        (running,) = queue_runs_for_track(self.track(utc(5, 0)))
        claim_next_run("riskscape.nz")
        (other,) = queue_runs_for_track(self.track(utc(5, 0), name="2026040500_WINSTON"))
        queue_runs_for_track(self.track(utc(5, 6)))
        running.refresh_from_db()
        other.refresh_from_db()
        self.assertEqual(running.status, ModelRun.RUNNING)
        self.assertEqual(other.status, ModelRun.QUEUED)


class ClaimTests(QueueTestCase):
    def test_claims_oldest_run_for_this_server_only(self):
        first = queue_runs_for_track(self.track(utc(6), name="2026040600_A"))[0]
        queue_runs_for_track(self.track(utc(6), name="2026040600_B"))
        queue_runs_for_track(self.track(utc(22), track_type=self.best))

        claimed = claim_next_run("riskscape.nz")
        self.assertEqual(claimed.pk, first.pk)
        self.assertEqual((claimed.status, claimed.claimed_by, claimed.attempts), (ModelRun.RUNNING, "riskscape.nz", 1))
        self.assertIsNone(claim_next_run("some-other-server"))

    def test_nothing_queued_returns_none(self):
        self.assertIsNone(claim_next_run("riskscape.nz"))

    def test_stale_run_is_requeued_then_failed_after_max_attempts(self):
        (run,) = queue_runs_for_track(self.track(utc(7)))
        expired = timezone.now() - datetime.timedelta(seconds=LEASE_SECONDS + 60)
        for attempt in range(1, MAX_ATTEMPTS + 1):
            self.assertEqual(claim_next_run("riskscape.nz").pk, run.pk)
            ModelRun.objects.filter(pk=run.pk).update(claimed_at=expired)
        self.assertIsNone(claim_next_run("riskscape.nz"))
        run.refresh_from_db()
        self.assertEqual((run.status, run.attempts), (ModelRun.FAILED, MAX_ATTEMPTS))


class PublishTests(QueueTestCase):
    def test_complete_run_creates_event_with_its_outputs(self):
        run, risk, event = self.run_to_completion(self.track(utc(8, 0)))
        run.refresh_from_db()
        self.assertEqual((run.status, run.model_run_completed), (ModelRun.COMPLETE, True))
        self.assertEqual(event.title, "Tropical Cyclone Vaianu")
        self.assertEqual((event.country, event.model_run, event.cyclone_track), (self.vu, run, run.tc_track))
        self.assertEqual(list(event.risks.all()), [risk])

    def test_newer_run_replaces_model_outputs_but_keeps_manual_risks(self):
        _, old_risk, event = self.run_to_completion(self.track(utc(8, 0)))
        manual = RiskInformation.objects.create(
            title="hand-linked", country=self.vu, upload=SimpleUploadedFile("m.geojson", GEOJSON)
        )
        event.risks.add(manual)

        new_run, new_risk, new_event = self.run_to_completion(self.track(utc(8, 6)))
        self.assertEqual(new_event.pk, event.pk)
        self.assertCountEqual(new_event.risks.all(), [manual, new_risk])
        self.assertEqual(new_event.model_run, new_run)
        self.assertEqual(Event.objects.count(), 1)

    def test_late_finishing_older_forecast_does_not_override_newer(self):
        (old,) = queue_runs_for_track(self.track(utc(9, 0)))
        claim_next_run("riskscape.nz")
        new_run, new_risk, _ = self.run_to_completion(self.track(utc(9, 6)))

        self.risk(old, "stale")
        event = finish_run(old, ModelRun.COMPLETE)
        self.assertEqual(event.model_run, new_run)
        self.assertEqual(list(event.risks.all()), [new_risk])

    def test_best_track_overrides_forecast(self):
        self.run_to_completion(self.track(utc(10, 0)))
        best_run, best_risk, event = self.run_to_completion(self.track(utc(10, 0), track_type=self.best))
        self.assertEqual(event.model_run, best_run)
        self.assertEqual(list(event.risks.all()), [best_risk])

    def test_reuses_imported_event_for_same_storm(self):
        lola_type, _ = EventType.objects.get_or_create(event_name="Tropical Cyclone")
        imported_track = CycloneTrack.objects.create(
            cyclone_name="Vaianu", country=self.vu, track_file=SimpleUploadedFile("t.geojson", GEOJSON)
        )
        imported = Event.objects.create(
            title="TC Vaianu", event_datetime=utc(8), event_type=lola_type, country=self.vu, cyclone_track=imported_track
        )
        _, _, event = self.run_to_completion(self.track(utc(8, 12)))
        self.assertEqual(event.pk, imported.pk)

    def test_same_name_years_apart_is_a_new_event(self):
        _, _, first = self.run_to_completion(self.track(utc(8, 0)))
        later = utc(8).replace(year=2031)
        _, _, second = self.run_to_completion(self.track(later, name=f"{later:%Y%m%d%H}_VAIANU"))
        self.assertNotEqual(first.pk, second.pk)

    def test_failed_run_publishes_nothing(self):
        (run,) = queue_runs_for_track(self.track(utc(11)))
        claim_next_run("riskscape.nz")
        self.assertIsNone(finish_run(run, ModelRun.FAILED, error="boom"))
        run.refresh_from_db()
        self.assertEqual((run.status, run.model_run_completed, run.error), (ModelRun.FAILED, False, "boom"))
        self.assertFalse(Event.objects.exists())


class QueueApiTests(QueueTestCase):
    def setUp(self):
        super().setUp()
        self.client.force_authenticate(get_user_model().objects.create_user("runner"))

    def test_posting_a_track_queues_runs(self):
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                "/partner_api/v1/cyclone_track/",
                {
                    "cyclone_name": "2026041200_VAIANU",
                    "issued_time": "2026-04-12T00:00:00Z",
                    "cyclone_track_type": self.forecast.pk,
                    "country": self.vu.pk,
                    "track_file": SimpleUploadedFile("track.geojson", GEOJSON),
                },
                format="multipart",
            )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.content)
        self.assertEqual(ModelRun.objects.filter(tc_track=response.data["id"], status=ModelRun.QUEUED).count(), 1)

    def test_claim_then_finish(self):
        queue_runs_for_track(self.track(utc(12)))
        response = self.client.post("/partner_api/v1/model_run/claim/", {"server_name": "riskscape.nz"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        job = response.data
        self.assertEqual(job["model"]["name"], "TC-Impact")
        self.assertEqual(job["model"]["location"], "niwa/partner/tc")
        self.assertEqual(job["track"]["storm_name"], "VAIANU")
        self.assertEqual(job["track"]["country_code"], "VU")
        self.assertTrue(job["track"]["track_file"].startswith("http"))

        self.assertEqual(
            self.client.post("/partner_api/v1/model_run/claim/", {"server_name": "riskscape.nz"}).status_code,
            status.HTTP_204_NO_CONTENT,
        )

        self.risk(ModelRun.objects.get(pk=job["id"]))
        url = f"/partner_api/v1/model_run/{job['id']}/finish/"
        response = self.client.post(url, {"status": "complete", "external_url": "https://riskscape.nz/x"})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.content)
        self.assertIsNotNone(response.data["event"])

        # Finishing twice is a conflict, not a silent overwrite
        self.assertEqual(self.client.post(url, {"status": "failed"}).status_code, status.HTTP_409_CONFLICT)

    def test_queue_endpoints_require_auth(self):
        self.client.force_authenticate(None)
        response = self.client.post("/partner_api/v1/model_run/claim/", {"server_name": "riskscape.nz"})
        self.assertIn(response.status_code, (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN))

    def test_operations_status_explains_when_a_queued_run_has_no_runner(self):
        queue_runs_for_track(self.track(utc(13)))
        response = self.client.get("/partner_api/v1/model_run/operations/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        runner = next(r for r in response.data["runners"] if r["server_name"] == "riskscape.nz")
        self.assertEqual(runner["server_name"], "riskscape.nz")
        self.assertFalse(runner["online"])
        self.assertEqual(runner["queued_runs"], 1)
        self.assertIn("No runner heartbeat", runner["message"])

    def test_heartbeat_marks_the_matching_runner_ready(self):
        response = self.client.post(
            "/partner_api/v1/model_run/heartbeat/",
            {"server_name": "riskscape.nz", "backend": "platform", "hostname": "runner-01"},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        heartbeat = RunnerHeartbeat.objects.get(server_name="riskscape.nz")
        self.assertEqual((heartbeat.backend, heartbeat.hostname), ("platform", "runner-01"))

        response = self.client.get("/partner_api/v1/model_run/operations/")
        runner = next(r for r in response.data["runners"] if r["server_name"] == "riskscape.nz")
        self.assertTrue(runner["online"])
        self.assertEqual(runner["message"], "Ready to claim queued runs")


class ChainTests(QueueTestCase):
    def make_chain(self):
        # self.onprem is the best-track trigger's model: make it the head of
        # surge -> wind -> impact, all on the same runner
        wind = Model.objects.create(name="Wind Hazard", server_name="onprem-1")
        impact = Model.objects.create(name="RiskScape", server_name="onprem-1")
        self.onprem.next_model = wind
        self.onprem.save()
        wind.next_model = impact
        wind.save()
        return [self.onprem, wind, impact]

    def test_chain_follows_next_model_and_stops_at_a_cycle(self):
        chain = self.make_chain()
        self.assertEqual(self.onprem.chain(), chain)
        chain[2].next_model = self.onprem
        chain[2].save()
        self.assertEqual(self.onprem.chain(), chain)

    def test_best_track_queues_the_whole_chain_in_order(self):
        chain = self.make_chain()
        (run,) = queue_runs_for_track(self.track(utc(22), track_type=self.best))
        self.assertEqual(ordered_run_models(run), chain)

    def test_claimed_job_lists_the_chain_in_order(self):
        self.make_chain()
        queue_runs_for_track(self.track(utc(22), track_type=self.best))
        self.client.force_authenticate(get_user_model().objects.create_user("runner"))
        job = self.client.post("/partner_api/v1/model_run/claim/", {"server_name": "onprem-1"}).data
        self.assertEqual([m["name"] for m in job["models"]], [self.onprem.name, "Wind Hazard", "RiskScape"])
        self.assertEqual(job["model"]["name"], self.onprem.name)

    def test_single_model_job_still_has_models_list(self):
        (run,) = queue_runs_for_track(self.track(utc(23)))
        self.assertEqual(ordered_run_models(run), [self.rapid])


class SetupTcPipelinesTests(APITestCase):
    def run_setup(self, **options):
        out = io.StringIO()
        call_command("setup_tc_pipelines", stdout=out, **options)
        return out.getvalue()

    def test_creates_rapid_model_pdna_chain_and_triggers(self):
        output = self.run_setup(create_track_types=True)
        self.assertIn("QuickSurge -> Wind Hazard -> RiskScape on lotgemdev01", output)

        rapid = Model.objects.get(server_name="riskscape.nz")
        self.assertEqual((rapid.name, rapid.location), ("Rapid-Exposure-Forecast-GeoJSON", "niwa/partner/rsmc-tcwc"))
        # The model reads the track from `hazard` as a plain URL, in the ISO-UTC form it can parse
        self.assertEqual(rapid.inputs, {"hazard": "{model_track_url}"})

        surge = Model.objects.get(name="QuickSurge", server_name="lotgemdev01")
        self.assertEqual([m.name for m in surge.chain()], ["QuickSurge", "Wind Hazard", "RiskScape"])
        for model in surge.chain()[:2]:
            self.assertNotIn("-ti", model.run_command)
            self.assertIn("CYCLONE_TRACK_URL={track_url}", model.run_command)

        triggers = {t.cyclone_track_type.name: t.model.name for t in ModelTrigger.objects.all()}
        self.assertEqual(
            triggers,
            {"Official Forecast Track": "Rapid-Exposure-Forecast-GeoJSON", "Post-event Best Track": "QuickSurge"},
        )

    def test_rerun_updates_instead_of_duplicating(self):
        self.run_setup(create_track_types=True)
        self.run_setup(rapid_project="niwa/partner/tc-impact")
        self.assertEqual(Model.objects.count(), 4)
        self.assertEqual(ModelTrigger.objects.count(), 2)
        self.assertEqual(Model.objects.get(server_name="riskscape.nz").location, "niwa/partner/tc-impact")

    def test_missing_track_types_are_reported(self):
        with self.assertRaisesMessage(CommandError, "No cyclone track type with 'forecast'"):
            self.run_setup()


TRACK_CSV = (
    b"Time,Latitude,Longitude\n"
    b"2026-04-22T00:00:00Z,-15.0,167.0\n"
    b"2026-04-22T06:00:00Z,-15.5,167.6\n"
    b"2026-04-22T12:00:00Z,-16.1,168.2\n"
)


class OperatorApiTests(QueueTestCase):
    def setUp(self):
        super().setUp()
        self.admin = get_user_model().objects.create_superuser("operator", password="x")
        self.client.force_authenticate(self.admin)

    def submit(self, track_type, country=None, file_bytes=TRACK_CSV, name="2026042200_VAIANU"):
        return self.client.post(
            "/partner_api/v1/operator/tracks/",
            {
                "cyclone_name": name,
                "issued_time": "2026-04-22T00:00:00Z",
                "cyclone_track_type": track_type.pk,
                "country": (country or self.vu).pk,
                "track_file": SimpleUploadedFile("track.csv", file_bytes, content_type="text/csv"),
            },
            format="multipart",
        )

    def test_only_superusers_can_use_operator_endpoints(self):
        self.client.force_authenticate(get_user_model().objects.create_user("viewer"))
        self.assertEqual(self.client.get("/partner_api/v1/operator/me/").data["is_superuser"], False)
        for method, url in (
            ("get", "/partner_api/v1/operator/pipelines/"),
            ("post", "/partner_api/v1/operator/tracks/"),
            ("get", "/partner_api/v1/operator/runs/?track=1"),
        ):
            self.assertEqual(getattr(self.client, method)(url).status_code, status.HTTP_403_FORBIDDEN, url)

    def test_pipelines_describe_each_track_type_and_its_runner(self):
        RunnerHeartbeat.objects.create(server_name="riskscape.nz", backend="platform")
        data = self.client.get("/partner_api/v1/operator/pipelines/").data
        by_name = {t["name"]: t for t in data["track_types"]}
        rapid = by_name["Official Forecast Track"]
        self.assertEqual(rapid["label"], "Rapid (forecast)")
        self.assertEqual(rapid["pipelines"][0]["models"], ["TC-Impact"])
        self.assertTrue(rapid["pipelines"][0]["runner"]["online"])
        pdna = by_name["Post-event Best Track"]
        self.assertEqual(pdna["label"], "PDNA (post-event best track)")
        self.assertFalse(pdna["pipelines"][0]["runner"]["online"])
        self.assertIn("VU", [c["code"] for c in data["countries"]])

    def test_submitting_a_best_track_queues_the_pdna_run(self):
        response = self.submit(self.best)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(response.data["track"]["points"], 3)
        (run,) = response.data["runs"]
        self.assertEqual((run["status"], run["models"]), ("queued", ["TC-Impact-Detailed"]))
        self.assertEqual(run["runner"]["server_name"], "onprem-1")
        self.assertIsNone(response.data["message"])

    def test_a_file_without_track_points_is_rejected_and_not_kept(self):
        before = CycloneTrack.objects.count()
        response = self.submit(self.best, file_bytes=b"Time,Latitude,Longitude\n")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("No track points", response.data["detail"])
        self.assertEqual(CycloneTrack.objects.count(), before)
        self.assertFalse(ModelRun.objects.exists())

    def test_a_track_type_without_a_pipeline_says_so(self):
        analysis = CycloneTrackType.objects.create(name="Analysis Track")
        response = self.submit(analysis)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["runs"], [])
        self.assertIn("no enabled pipeline runs Analysis Track tracks for Vanuatu", response.data["message"])

    def test_runs_endpoint_reports_status_and_logs(self):
        track_id = self.submit(self.best).data["track"]["id"]
        run = ModelRun.objects.get(tc_track_id=track_id)
        claim_next_run("onprem-1")
        run.logs.create(logs="Step 1/1: TC-Impact-Detailed started")
        (summary,) = self.client.get(f"/partner_api/v1/operator/runs/?track={track_id}").data["runs"]
        self.assertEqual(summary["status"], "running")
        self.assertEqual(summary["logs"], ["Step 1/1: TC-Impact-Detailed started"])


class AdminRunTests(QueueTestCase):
    CHANGELIST = "/partner_api/admin/cyclone_track/cyclonetrack/"

    def setUp(self):
        super().setUp()
        self.client.force_login(get_user_model().objects.create_superuser("admin-user", password="x"))

    def csv_track(self, file_bytes=TRACK_CSV):
        return CycloneTrack.objects.create(
            cyclone_name="2026042221_VAIANU",
            issued_time=utc(22, 21),
            cyclone_track_type=self.best,
            country=self.vu,
            track_file=SimpleUploadedFile("track.csv", file_bytes),
        )

    def run_action(self, track):
        response = self.client.post(
            self.CHANGELIST, {"action": "queue_model_runs", "_selected_action": [track.pk]}, follow=True
        )
        return [str(m) for m in response.context["messages"]]

    def test_action_says_which_pipeline_and_runner(self):
        track = self.csv_track()
        (message,) = self.run_action(track)
        run = ModelRun.objects.get(tc_track=track)
        self.assertIn(f"Queued PDNA (post-event best track) run {run.pk}", message)
        self.assertIn("(TC-Impact-Detailed) on onprem-1; its runner is offline", message)

    def test_running_a_track_again_points_to_requeue(self):
        track = self.csv_track()
        self.run_action(track)
        (message,) = self.run_action(track)
        self.assertIn("already has run", message)
        self.assertIn("Requeue selected runs", message)
        self.assertEqual(ModelRun.objects.filter(tc_track=track).count(), 1)

    def test_a_track_without_points_is_reported_not_queued(self):
        track = self.csv_track(file_bytes=b"Time,Latitude,Longitude\n")
        (message,) = self.run_action(track)
        self.assertIn("no track points could be read", message)
        self.assertFalse(ModelRun.objects.exists())

    def add_track(self, start_runs):
        data = {
            "cyclone_name": "2026042221_VAIANU",
            "issued_time_0": "2026-04-22",
            "issued_time_1": "21:30:00",
            "cyclone_track_type": self.best.pk,
            "country": self.vu.pk,
            "track_file": SimpleUploadedFile("track.csv", TRACK_CSV),
        }
        if start_runs:
            data["start_runs"] = "on"
        return self.client.post(self.CHANGELIST + "add/", data, follow=True)

    def test_start_runs_checkbox_queues_on_save(self):
        response = self.add_track(start_runs=True)
        messages = [str(m) for m in response.context["messages"]]
        self.assertTrue(any("Queued PDNA (post-event best track) run" in m for m in messages), messages)
        self.assertEqual(ModelRun.objects.count(), 1)

    def test_saving_without_the_checkbox_starts_nothing(self):
        self.add_track(start_runs=False)
        self.assertEqual(CycloneTrack.objects.filter(cyclone_name="2026042221_VAIANU").count(), 1)
        self.assertFalse(ModelRun.objects.exists())


class ForecastRunHistoryTests(QueueTestCase):
    """Each advisory's run stays reachable even though the Event shows only the latest."""

    def setUp(self):
        super().setUp()
        self.rapid.name = "Rapid exposure forecast"
        self.rapid.save()

    def event_row(self, event):
        response = self.client.get("/partner_api/v1/event/")
        results = response.data["results"] if "results" in response.data else response.data
        return next(r for r in results if r["id"] == event.pk)

    def test_event_lists_every_advisory_run_newest_first(self):
        run_a, risk_a, event = self.run_to_completion(self.track(utc(8, 12)))
        run_b, risk_b, same_event = self.run_to_completion(self.track(utc(8, 18)))
        self.assertEqual(event.pk, same_event.pk)

        runs = self.event_row(event)["forecast_runs"]
        self.assertEqual([r["id"] for r in runs], [run_b.pk, run_a.pk])
        self.assertEqual([r["is_current"] for r in runs], [True, False])
        self.assertEqual(runs[0]["risks"], [risk_b.pk])
        self.assertEqual(runs[1]["risks"], [risk_a.pk])  # kept after the Event moved on
        self.assertEqual(runs[1]["status"], ModelRun.COMPLETE)

    def test_other_storms_and_countries_are_not_listed(self):
        _, _, event = self.run_to_completion(self.track(utc(8, 12)))
        self.run_to_completion(self.track(utc(8, 13), name="2026040813_OTHER"))
        self.run_to_completion(self.track(utc(8, 14), country=self.to))
        runs = self.event_row(event)["forecast_runs"]
        self.assertEqual(len(runs), 1)

    def test_a_failed_advisory_is_listed_as_failed(self):
        run_a, _, event = self.run_to_completion(self.track(utc(8, 12)))
        (run_b,) = queue_runs_for_track(self.track(utc(8, 18)))
        claim_next_run(run_b.models.get().server_name)
        finish_run(run_b, ModelRun.FAILED, error="model dropped the track")
        runs = self.event_row(event)["forecast_runs"]
        self.assertEqual([r["status"] for r in runs], [ModelRun.FAILED, ModelRun.COMPLETE])
        self.assertEqual(runs[0]["error"], "model dropped the track")
        self.assertEqual([r["is_current"] for r in runs], [False, True])

    def test_a_hand_loaded_event_has_no_runs(self):
        event_type = EventType.objects.create(event_name="Tropical Cyclone")
        track = self.track(utc(8, 12))
        event = Event.objects.create(title="By hand", event_type=event_type, country=self.vu, cyclone_track=track)
        self.assertEqual(self.event_row(event)["forecast_runs"], [])


def write_ref_outputs(directory, prefix="test track-69-"):
    """A folder of REF outputs, as the importer reads them (platform downloads prefix each name)."""
    region = {"Region.Region": "Efate", "Exposed_Population": 120, "Max_WindSpeed_kmph": 130}
    feature = {"type": "Feature", "properties": region, "geometry": {"type": "Point", "coordinates": [168, -17]}}
    files = {
        "regional-impacts.geojson": json.dumps({"type": "FeatureCollection", "features": [feature]}),
        "exposure-by-cluster.geojson": json.dumps({"type": "FeatureCollection", "features": [feature]}),
        "regional-summary.csv": "﻿Country,Region,Exposed_Population\nVanuatu,,120\nVanuatu,Shefa,120\n",
        "total-exposed-by-country.csv": "Country,Overall_Winds,Exposed_Population\nVanuatu,Hurricane Winds,120\n",
        "total-by-windspeed.csv": "WindSpeed,Danger,Country,Population\n118,Hurricane Winds,Vanuatu,120\n",
        "total-by-track-distance.csv": "Track_distance,km,Country,Population\nWithin 25km of track,25,Vanuatu,120\n",
        "wind-swaths.geojson": json.dumps({"type": "FeatureCollection", "features": [feature]}),
        "track-distance.geojson": json.dumps({"type": "FeatureCollection", "features": [feature]}),
        "floodplain-exposure.geojson": json.dumps({"type": "FeatureCollection", "features": [feature]}),
        "cyclone-track.geojson": json.dumps(
            {
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "properties": {},
                        "geometry": {"type": "LineString", "coordinates": [[169.4, -12.7], [168.0, -16.0]]},
                    }
                ],
            }
        ),
    }
    for name, text in files.items():
        with open(f"{directory}/{prefix}{name}", "w", encoding="utf-8") as handle:
            handle.write(text)


class ImportRefRunTests(QueueTestCase):
    PREFIX = "test track-69-"

    def setUp(self):
        super().setUp()
        import tempfile

        EventType.objects.create(event_name="Tropical Cyclone")
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        write_ref_outputs(self.folder.name, self.PREFIX)

    def load(self, title="TC Lola REF", country="VU", **options):
        out = io.StringIO()
        call_command("import_ref_run", self.folder.name, country=country, title=title, stdout=out, **options)
        return out.getvalue()

    def test_loads_outputs_as_an_event_with_a_track_from_the_run(self):
        self.load(event_time="2023-10-23T03:00:00")

        event = Event.objects.get(title="TC Lola REF")
        self.assertEqual(event.country, self.vu)
        self.assertEqual(event.event_datetime, datetime.datetime(2023, 10, 23, 3, tzinfo=datetime.timezone.utc))
        names = sorted(risk.upload.name.split("/")[-1].split("_")[0].split(".")[0] for risk in event.risks.all())
        self.assertEqual(
            names,
            sorted(
                [
                    "cyclone-track",
                    "regional-impacts",
                    "regional-summary",
                    "total-exposed-by-country",
                    "total-by-windspeed",
                    "total-by-track-distance",
                    "exposure-by-cluster",
                    "wind-swaths",
                    "track-distance",
                    "floodplain-exposure",
                ]
            ),
        )
        summary = event.risks.get(upload__contains="regional-summary")
        self.assertEqual(summary.geometry[1]["Region"], "Shefa")
        self.assertIn("Country", summary.geometry[0])  # not "\ufeffCountry"
        self.assertEqual(summary.dataset_type, "regional_summary")
        self.assertEqual(
            event.risks.get(upload__contains="cyclone-track").dataset_type,
            "forecast_cyclone_track",
        )

        track = event.cyclone_track
        self.assertTrue(track.geometry_computed)
        self.assertEqual(
            [f["geometry"]["coordinates"] for f in track.geometry["features"]], [[169.4, -12.7], [168.0, -16.0]]
        )

    def test_refuses_a_run_whose_track_output_is_empty(self):
        with open(f"{self.folder.name}/{self.PREFIX}cyclone-track.geojson", "w", encoding="utf-8") as handle:
            handle.write(json.dumps({"type": "FeatureCollection", "features": []}))
        with self.assertRaisesMessage(CommandError, "dropped every track point"):
            self.load(track=self.track(utc(22), name="Wati").pk)
        self.assertFalse(Event.objects.filter(title="TC Lola REF").exists())

    def test_refuses_a_run_whose_geopackage_track_has_no_rows(self):
        import os
        import sqlite3

        os.remove(f"{self.folder.name}/{self.PREFIX}cyclone-track.geojson")
        with sqlite3.connect(f"{self.folder.name}/{self.PREFIX}cyclone-track.gpkg") as db:
            db.execute("CREATE TABLE gpkg_contents (table_name TEXT)")
            db.execute("INSERT INTO gpkg_contents VALUES ('cyclone-track')")
            db.execute('CREATE TABLE "cyclone-track" (fid INTEGER)')
        with self.assertRaisesMessage(CommandError, "dropped every track point"):
            self.load(track=self.track(utc(22), name="Wati").pk)

    def events_api(self):
        response = self.client.get("/partner_api/v1/event/")
        return response.data["results"] if "results" in response.data else response.data

    def test_as_run_puts_every_advisory_under_one_storm_event(self):
        older = self.track(utc(8, 12), name="2026040812_WATI")
        newer = self.track(utc(8, 18), name="2026040818_WATI")
        self.load(title="Wati 12Z", track=older.pk, as_run=True)
        self.load(title="Wati 18Z", track=newer.pk, as_run=True)

        self.assertEqual(Event.objects.count(), 1)
        event = Event.objects.get()
        self.assertEqual(event.title, "Tropical Cyclone Wati")
        self.assertEqual(event.cyclone_track, newer)
        row = self.events_api()[0]
        self.assertTrue(row["is_forecast"])
        runs = row["forecast_runs"]
        self.assertEqual([r["cyclone_track"] for r in runs], [newer.pk, older.pk])
        self.assertEqual([r["is_current"] for r in runs], [True, False])
        self.assertEqual(sorted(event.risks.values_list("pk", flat=True)), runs[0]["risks"])
        self.assertEqual(len(runs[1]["risks"]), len(runs[0]["risks"]))  # the older run keeps its own outputs
        self.assertEqual(set(runs[0]["risks"]) & set(runs[1]["risks"]), set())

    def test_as_run_keeps_the_newer_advisory_current_when_an_older_one_arrives_later(self):
        older = self.track(utc(8, 12), name="2026040812_WATI")
        newer = self.track(utc(8, 18), name="2026040818_WATI")
        self.load(title="Wati 18Z", track=newer.pk, as_run=True)
        before = sorted(Event.objects.get().risks.values_list("pk", flat=True))
        out = self.load(title="Wati 12Z", track=older.pk, as_run=True)

        self.assertIn("kept alongside a newer run", out)
        event = Event.objects.get()
        self.assertEqual(event.cyclone_track, newer)
        self.assertEqual(sorted(event.risks.values_list("pk", flat=True)), before)
        self.assertEqual(len(self.events_api()[0]["forecast_runs"]), 2)

    def test_as_run_files_results_under_the_given_country_not_the_tracks(self):
        # SPC files a track under the country it was issued for (Fiji); its results can be for another
        track = self.track(utc(8, 12), name="2026040812_WATI")  # a Vanuatu track
        self.load(title="Wati 12Z", country="TO", track=track.pk, as_run=True)

        event = Event.objects.get()
        self.assertEqual(event.country, self.to)
        self.assertEqual(len(self.events_api()[0]["forecast_runs"]), 1)

    def test_as_run_needs_a_track_and_will_not_import_a_track_twice_without_replace(self):
        with self.assertRaisesMessage(CommandError, "--as-run needs --track"):
            self.load(as_run=True)
        track = self.track(utc(8, 12), name="2026040812_WATI")
        self.load(track=track.pk, as_run=True)
        with self.assertRaisesMessage(CommandError, "already imported"):
            self.load(track=track.pk, as_run=True)
        self.load(track=track.pk, as_run=True, replace=True)
        self.assertEqual(ModelRun.objects.filter(tc_track=track).count(), 1)
        self.assertEqual(RiskInformation.objects.count(), 10)  # the first import's outputs went with it
        self.assertEqual(Event.objects.count(), 1)

    def test_uses_an_existing_track(self):
        track = self.track(utc(22), name="Lola")
        self.load(track=track.pk)
        event = Event.objects.get(title="TC Lola REF")
        self.assertEqual(event.cyclone_track, track)
        self.assertEqual(event.event_datetime, track.issued_time)

    def test_same_title_needs_replace_and_replace_cleans_up(self):
        self.load()
        with self.assertRaisesMessage(CommandError, "Pass --replace"):
            self.load()
        self.load(replace=True)
        self.assertEqual(Event.objects.filter(title="TC Lola REF").count(), 1)
        self.assertEqual(RiskInformation.objects.count(), 10)
        self.assertEqual(CycloneTrack.objects.count(), 1)

    def test_an_empty_overlay_is_skipped_not_an_error(self):
        with open(f"{self.folder.name}/{self.PREFIX}floodplain-exposure.geojson", "w", encoding="utf-8") as handle:
            handle.write('{"type":"FeatureCollection","features":[]}')
        out = self.load()
        self.assertIn("Skipped empty overlays: floodplain-exposure.geojson", out)
        event = Event.objects.get(title="TC Lola REF")
        self.assertFalse(event.risks.filter(upload__contains="floodplain-exposure").exists())
        self.assertTrue(event.risks.filter(upload__contains="regional-impacts").exists())

    def test_an_empty_required_output_still_fails(self):
        with open(f"{self.folder.name}/{self.PREFIX}regional-impacts.geojson", "w", encoding="utf-8") as handle:
            handle.write('{"type":"FeatureCollection","features":[]}')
        with self.assertRaisesMessage(CommandError, "couldn't be read"):
            self.load()

    def test_map_overlays_are_optional(self):
        import os

        for name in ("wind-swaths.geojson", "track-distance.geojson", "floodplain-exposure.geojson"):
            os.remove(f"{self.folder.name}/{self.PREFIX}{name}")
        self.load()
        # The REF input track is preserved even when no optional map overlays
        # were provided, alongside the six required REF output files.
        self.assertEqual(Event.objects.get(title="TC Lola REF").risks.count(), 7)

    def test_rejects_a_folder_without_ref_outputs(self):
        import os

        os.remove(f"{self.folder.name}/{self.PREFIX}total-by-windspeed.csv")
        with self.assertRaisesMessage(CommandError, "missing: total-by-windspeed.csv"):
            self.load()


class ModelReadyTrackTests(QueueTestCase):
    """The track the RiskScape models read: SPC's space-separated times made ISO-UTC."""

    def make(self, features):
        track = self.track(utc(17, 12), name="2026101712_WATI")
        CycloneTrack.objects.filter(pk=track.pk).update(
            geometry={"type": "FeatureCollection", "features": features}, geometry_computed=True
        )
        return track

    def point(self, time, **props):
        return {
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [-162.9, -14.4]},
            "properties": {"Time": time, "MEAN_WIND": 65, **props},
        }

    def test_times_become_iso_utc_and_everything_else_is_untouched(self):
        track = self.make([self.point("2026-10-17 12:00:00"), self.point("2026-10-17T18:00:00Z")])
        response = self.client.get(f"/partner_api/v1/cyclone_track/{track.pk}/model_input/")
        self.assertEqual(response.status_code, 200)
        features = response.json()["features"]
        self.assertEqual([f["properties"]["Time"] for f in features], ["2026-10-17T12:00:00Z", "2026-10-17T18:00:00Z"])
        self.assertEqual(features[0]["properties"]["MEAN_WIND"], 65)
        self.assertEqual(features[0]["geometry"]["coordinates"], [-162.9, -14.4])

    def test_the_stored_track_is_not_changed(self):
        track = self.make([self.point("2026-10-17 12:00:00")])
        self.client.get(f"/partner_api/v1/cyclone_track/{track.pk}/model_input/")
        track.refresh_from_db()
        self.assertEqual(track.geometry["features"][0]["properties"]["Time"], "2026-10-17 12:00:00")

    def test_a_track_with_no_geometry_is_a_404(self):
        track = self.track(utc(17, 12))
        CycloneTrack.objects.filter(pk=track.pk).update(geometry=None)
        self.assertEqual(self.client.get(f"/partner_api/v1/cyclone_track/{track.pk}/model_input/").status_code, 404)

    def test_time_helper_handles_the_layouts_seen(self):
        from cyclone_track.model_input import iso_utc_time

        self.assertEqual(iso_utc_time("2026-10-17 12:00:00"), "2026-10-17T12:00:00Z")
        self.assertEqual(iso_utc_time("2026-10-17T12:00:00Z"), "2026-10-17T12:00:00Z")
        self.assertEqual(iso_utc_time("2026-10-17T12:00:00"), "2026-10-17T12:00:00Z")
        self.assertIsNone(iso_utc_time(None))
        self.assertEqual(iso_utc_time("not a time"), "not a time")


class AutomatedForecastTests(QueueTestCase):
    """What the automated runner needs: the real model's name, and the results' country."""

    def event_row(self, event):
        response = self.client.get("/partner_api/v1/event/")
        results = response.data["results"] if "results" in response.data else response.data
        return next(r for r in results if r["id"] == event.pk)

    def test_the_real_model_id_with_hyphens_is_recognised_as_a_forecast(self):
        self.rapid.name = "Rapid-Exposure-Forecast-GeoJSON"
        self.rapid.save()
        run, _, event = self.run_to_completion(self.track(utc(8, 12), name="2026040812_WATI"))
        row = self.event_row(event)
        self.assertTrue(row["is_forecast"])
        self.assertEqual([r["id"] for r in row["forecast_runs"]], [run.pk])

    def test_an_unrelated_model_name_is_not_a_forecast(self):
        self.rapid.name = "TC-Impact"
        self.rapid.save()
        _, _, event = self.run_to_completion(self.track(utc(8, 12)))
        self.assertFalse(self.event_row(event)["is_forecast"])

    def test_the_event_goes_to_the_country_the_results_are_for_not_the_tracks(self):
        # SPC filed the Wati tracks under Fiji; the exposure is in the Cook Islands
        track = self.track(utc(8, 12), name="2026040812_WATI")  # a Vanuatu track
        (run,) = queue_runs_for_track(track)
        claim_next_run(run.models.get().server_name)
        self.risk(run, country=self.to)
        event = finish_run(run, ModelRun.COMPLETE)
        self.assertEqual(event.country, self.to)

    def test_an_event_still_follows_the_track_when_outputs_name_no_country(self):
        track = self.track(utc(8, 12), name="2026040812_WATI")
        (run,) = queue_runs_for_track(track)
        claim_next_run(run.models.get().server_name)
        RiskInformation.objects.create(
            title="impact", model_run=run, upload=SimpleUploadedFile("impact.geojson", GEOJSON)
        )
        self.assertEqual(finish_run(run, ModelRun.COMPLETE).country, self.vu)


def load_tc_runner():
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parent.parent / "runners" / "tc_runner.py"
    spec = importlib.util.spec_from_file_location("tc_runner_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TcRunnerHelperTests(SimpleTestCase):
    def setUp(self):
        import tempfile

        self.runner = load_tc_runner()
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.out = __import__("pathlib").Path(self.dir.name)

    def write(self, name, text):
        (self.out / name).write_text(text, encoding="utf-8")

    def gpkg(self, name, rows):
        import sqlite3

        with sqlite3.connect(self.out / name) as db:
            db.execute("CREATE TABLE gpkg_contents (table_name TEXT)")
            db.execute("INSERT INTO gpkg_contents VALUES ('cyclone-track')")
            db.execute('CREATE TABLE "cyclone-track" (fid INTEGER)')
            for i in range(rows):
                db.execute('INSERT INTO "cyclone-track" VALUES (?)', (i,))

    def test_an_empty_track_output_fails_the_run(self):
        self.gpkg("cyclone-track.gpkg", 0)
        with self.assertRaisesMessage(self.runner.RunFailed, "dropped every track point"):
            self.runner.check_track_output(self.out)

    def test_an_empty_geojson_track_output_fails_the_run(self):
        self.write("cyclone-track.geojson", '{"type":"FeatureCollection","features":[]}')
        with self.assertRaises(self.runner.RunFailed):
            self.runner.check_track_output(self.out)

    def test_a_real_track_passes_even_if_it_missed_everything(self):
        self.gpkg("cyclone-track.gpkg", 1)
        self.runner.check_track_output(self.out)  # no exposure tables needed to pass

    def test_a_run_with_no_track_output_is_left_to_other_checks(self):
        self.write("regional-summary.csv", "Region\n")
        self.runner.check_track_output(self.out)

    def test_results_country_is_the_one_with_the_most_people_exposed(self):
        self.write(
            "total-exposed-by-country.csv",
            "Region,Overall_Winds,Exposed_Population\nCook Islands,Gale,2622\nTonga,Gale,40\nSamoa,Gale,0\n",
        )
        self.assertEqual(self.runner.results_country(self.out), ("Cook Islands", ["Tonga"]))

    def test_results_country_reads_a_country_column_and_a_bom(self):
        (self.out / "total-exposed-by-country.csv").write_bytes(
            "\ufeffCountry,Exposed_Population\nVanuatu,12\n".encode("utf-8")
        )
        self.assertEqual(self.runner.results_country(self.out), ("Vanuatu", []))

    def test_results_country_is_none_when_nothing_is_exposed_or_the_table_is_missing(self):
        self.assertEqual(self.runner.results_country(self.out), (None, []))
        self.write("total-exposed-by-country.csv", "Region,Exposed_Population\nCook Islands,0\n")
        self.assertEqual(self.runner.results_country(self.out), (None, []))

    def test_country_ids_match_long_or_short_names_ignoring_case(self):
        api = self.runner.PartnerApi("http://api.test", "token")
        api._countries = [
            {"id": 1, "long_name": "Fiji", "short_name": "FJI"},
            {"id": 3, "long_name": "Cook Islands", "short_name": "CK"},
        ]
        self.assertEqual(api.country_id("cook islands"), 3)
        self.assertEqual(api.country_id("CK"), 3)
        self.assertIsNone(api.country_id("Atlantis"))


from .direct_runner import load_tc_runner as direct_load_tc_runner  # noqa: E402


class FakePlatform:
    """Stands in for RiskScape: writes the outputs a run would produce into output_dir."""

    def __init__(self, track_points=1, exposed_country="Tonga", fail=False):
        self.track_points = track_points
        self.exposed_country = exposed_country
        self.fail = fail
        self.seen_variables = None

    def run(self, job, model, variables, output_dir):
        from pathlib import Path

        if self.fail:
            raise direct_load_tc_runner().RunFailed("RiskScape said no")
        self.seen_variables = dict(variables)
        out = Path(output_dir)
        coordinates = [[-162.9, -14.4], [-161.7, -16.5]][: self.track_points]
        feature = {"type": "Feature", "properties": {}, "geometry": {"type": "LineString", "coordinates": coordinates}}
        (out / "cyclone-track.geojson").write_text(
            json.dumps({"type": "FeatureCollection", "features": [feature] if coordinates else []})
        )
        (out / "regional-impacts.geojson").write_bytes(GEOJSON)
        (out / "regional-summary.csv").write_text("Region,Exposed_Population\nNiue,5\n")
        (out / "total-exposed-by-country.csv").write_text(
            f"Region,Overall_Winds,Exposed_Population\n{self.exposed_country},Gale,2622\n"
        )
        return "https://riskscape.nz/run/1"


class DirectRunnerTests(QueueTestCase):
    """The whole automatic chain, inside Django: queued -> claimed -> run -> checked -> published."""

    def setUp(self):
        super().setUp()
        self.rapid.name = "Rapid-Exposure-Forecast-GeoJSON"
        self.rapid.inputs = {"hazard": "{model_track_url}"}
        self.rapid.save()
        import tempfile

        self.work = tempfile.TemporaryDirectory()
        self.addCleanup(self.work.cleanup)

    def run_once(self, backend, **extra):
        from .direct_runner import run_queue

        return run_queue(
            server_name="riskscape.nz",
            backend_name="platform",
            backend=backend,
            work_dir=self.work.name,
            once=True,
            api=extra.pop("api", None),
            **extra,
        )

    def queue(self, name="2026040812_WATI", issued=None):
        track = self.track(issued or utc(8, 12), name=name)
        CycloneTrack.objects.filter(pk=track.pk).update(
            geometry={"type": "FeatureCollection", "features": [{"type": "Feature", "properties": {"Time": "2026-04-08 12:00:00"}, "geometry": {"type": "Point", "coordinates": [-162.9, -14.4]}}]},
            geometry_computed=True,
        )
        (run,) = queue_runs_for_track(track)
        return track, run

    def test_a_queued_run_is_claimed_run_and_published_under_the_results_country(self):
        track, run = self.queue()  # a Vanuatu track, with exposure in Tonga
        backend = FakePlatform(exposed_country="Tonga")
        self.assertTrue(self.run_once(backend))

        run.refresh_from_db()
        self.assertEqual(run.status, ModelRun.COMPLETE)
        self.assertEqual(run.external_url, "https://riskscape.nz/run/1")
        event = Event.objects.get()
        self.assertEqual(event.country, self.to)  # the results' country, not the track's
        self.assertEqual(event.model_run, run)
        self.assertEqual(event.risks.count(), 4)
        types = set(event.risks.values_list("dataset_type", flat=True))
        self.assertIn("regional_summary", types)
        self.assertTrue(all(r.country == self.to for r in event.risks.all()))

    def test_the_model_is_given_the_model_ready_track_link(self):
        track, _ = self.queue()
        backend = FakePlatform()
        self.run_once(backend)
        self.assertTrue(
            backend.seen_variables["model_track_url"].endswith(f"/cyclone_track/{track.pk}/model_input/")
        )

    def test_a_run_whose_model_dropped_the_track_fails_and_publishes_nothing(self):
        _, run = self.queue()
        self.run_once(FakePlatform(track_points=0))
        run.refresh_from_db()
        self.assertEqual(run.status, ModelRun.FAILED)
        self.assertIn("dropped every track point", run.error)
        self.assertFalse(Event.objects.exists())
        self.assertFalse(RiskInformation.objects.exists())

    def test_a_backend_error_marks_the_run_failed_with_the_reason(self):
        _, run = self.queue()
        self.run_once(FakePlatform(fail=True))
        run.refresh_from_db()
        self.assertEqual(run.status, ModelRun.FAILED)
        self.assertIn("RiskScape said no", run.error)
        self.assertNotIn("RunFailed:", run.error)  # a known failure, not an unexpected error

    def test_the_worker_checks_in_and_reports_an_empty_queue(self):
        self.assertFalse(self.run_once(FakePlatform()))
        beat = RunnerHeartbeat.objects.get(server_name="riskscape.nz")
        self.assertEqual(beat.backend, "platform")

    def test_two_advisories_end_up_as_runs_of_one_storm_event(self):
        self.queue(name="2026040812_WATI", issued=utc(8, 12))
        self.run_once(FakePlatform(exposed_country="Tonga"))
        self.queue(name="2026040818_WATI", issued=utc(8, 18))
        self.run_once(FakePlatform(exposed_country="Tonga"))
        self.assertEqual(Event.objects.count(), 1)
        response = self.client.get("/partner_api/v1/event/")
        results = response.data["results"] if "results" in response.data else response.data
        self.assertEqual(len(results[0]["forecast_runs"]), 2)

    def test_the_command_runs_once_with_the_platform_backend_it_is_given(self):
        from unittest import mock

        from .management.commands.run_model_queue import Command

        _, run = self.queue()
        with mock.patch.object(Command, "make_backend", return_value=FakePlatform()):
            call_command("run_model_queue", once=True, work_dir=self.work.name, stdout=io.StringIO())
        run.refresh_from_db()
        self.assertEqual(run.status, ModelRun.COMPLETE)

    def test_the_command_needs_a_riskscape_token(self):
        import os
        from unittest import mock

        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("RISKSCAPE_TOKEN", None)
            with self.assertRaisesMessage(CommandError, "RISKSCAPE_TOKEN is not set"):
                call_command("run_model_queue", once=True, work_dir=self.work.name, stdout=io.StringIO())


class RunnerHealthTests(QueueTestCase):
    def check(self, **options):
        out = io.StringIO()
        call_command("check_runner_health", stdout=out, **options)
        return out.getvalue()

    def test_a_runner_that_never_checked_in_is_reported(self):
        with self.assertRaisesMessage(CommandError, "riskscape.nz: has never checked in"):
            self.check()

    def test_a_recent_heartbeat_and_an_empty_queue_is_healthy(self):
        for server in ("riskscape.nz", "onprem-1"):
            RunnerHeartbeat.objects.update_or_create(server_name=server, defaults={"backend": "platform"})
        self.assertIn("healthy", self.check())

    def test_a_silent_runner_is_reported_with_how_long(self):
        for server in ("riskscape.nz", "onprem-1"):
            RunnerHeartbeat.objects.update_or_create(server_name=server, defaults={"backend": "platform"})
        RunnerHeartbeat.objects.filter(server_name="riskscape.nz").update(
            last_seen_at=timezone.now() - datetime.timedelta(minutes=20)
        )
        with self.assertRaisesMessage(CommandError, "riskscape.nz: no heartbeat for 20 min"):
            self.check()

    def test_a_run_stuck_in_the_queue_and_a_recent_failure_are_reported(self):
        for server in ("riskscape.nz", "onprem-1"):
            RunnerHeartbeat.objects.update_or_create(server_name=server, defaults={"backend": "platform"})
        (waiting,) = queue_runs_for_track(self.track(utc(8, 12)))
        ModelRun.objects.filter(pk=waiting.pk).update(created_at=timezone.now() - datetime.timedelta(minutes=90))
        (failing,) = queue_runs_for_track(self.track(utc(8, 18), name="2026040818_OTHER"))
        ModelRun.objects.filter(pk=failing.pk).update(
            status=ModelRun.FAILED, finished_at=timezone.now(), error="model dropped the track"
        )
        with self.assertRaises(CommandError) as raised:
            self.check()
        message = str(raised.exception)
        self.assertIn(f"run {waiting.pk}", message)
        self.assertIn("queued for 90 min", message)
        self.assertIn("model dropped the track", message)

    def test_it_emails_the_alert_list_only_when_asked(self):
        from django.core import mail
        from django.test import override_settings

        with override_settings(RUNNER_ALERT_EMAILS="ops@example.org, me@example.org"):
            with self.assertRaises(CommandError):
                self.check()
            self.assertEqual(len(mail.outbox), 0)
            with self.assertRaises(CommandError):
                self.check(email=True)
            self.assertEqual(len(mail.outbox), 1)
            self.assertEqual(mail.outbox[0].to, ["ops@example.org", "me@example.org"])


class CountryByCodeTests(QueueTestCase):
    """Runs and imports find a country by its code, so differently written names cannot misfile them."""

    def setUp(self):
        super().setUp()
        # The Cook Islands row migration 0003 seeds (and 0005 codes), named as the live API names it
        Country.objects.filter(code="CK").update(long_name="Cook Island")
        self.ck = Country.objects.get(code="CK")
        self.fj = Country.objects.create(short_name="FJI", long_name="FIJI", code="FJ")
        self.rapid.name = "Rapid-Exposure-Forecast-GeoJSON"
        self.rapid.inputs = {"hazard": "{model_track_url}"}
        self.rapid.save()
        import tempfile

        self.work = tempfile.TemporaryDirectory()
        self.addCleanup(self.work.cleanup)

    def ref_folder(self):
        write_ref_outputs(self.work.name)
        return self.work.name

    def test_results_for_cook_islands_reach_the_row_the_live_api_calls_cook_island(self):
        from .direct_runner import run_queue

        # SPC filed the track under Fiji; RiskScape's table says "Cook Islands"; the row says "Cook Island"
        track = self.track(utc(8, 12), name="2026040812_WATI", country=self.fj)
        CycloneTrack.objects.filter(pk=track.pk).update(
            geometry={"type": "FeatureCollection", "features": [{"type": "Feature", "properties": {}, "geometry": {"type": "Point", "coordinates": [-162.9, -14.4]}}]},
            geometry_computed=True,
        )
        queue_runs_for_track(track)
        run_queue(
            server_name="riskscape.nz", backend_name="platform", backend=FakePlatform(exposed_country="Cook Islands"),
            work_dir=self.work.name, once=True,
        )
        self.assertEqual(Event.objects.get().country, self.ck)

    def test_an_import_takes_a_country_code(self):
        EventType.objects.create(event_name="Tropical Cyclone")
        track = self.track(utc(8, 12), name="2026040812_WATI", country=self.fj)
        folder = self.ref_folder()
        out = io.StringIO()
        call_command("import_ref_run", folder, country="CK", title="Wati 12Z", track=track.pk, as_run=True, stdout=out)
        self.assertEqual(Event.objects.get().country, self.ck)

    def test_an_import_for_an_unknown_country_says_so(self):
        EventType.objects.create(event_name="Tropical Cyclone")
        folder = self.ref_folder()
        with self.assertRaisesMessage(CommandError, "No country matching 'XX'. Give its code, e.g. CK."):
            call_command("import_ref_run", folder, country="XX", title="x", stdout=io.StringIO())


class RunnerScriptCountryTests(SimpleTestCase):
    def setUp(self):
        self.runner = load_tc_runner()

    def api(self, rows):
        api = self.runner.PartnerApi("http://api.test", "token")
        api._countries = rows
        return api

    LIVE = [
        {"id": 1, "code": "FJ", "short_name": "FJI", "long_name": "FIJI"},
        {"id": 3, "code": "CK", "short_name": "CK", "long_name": "Cook Island"},
        {"id": 4, "code": "TO", "short_name": "Ton", "long_name": "Tonga"},
    ]

    def test_a_code_finds_its_country(self):
        self.assertEqual(self.api(self.LIVE).country_id("CK"), 3)
        self.assertEqual(self.api(self.LIVE).country_id("to"), 4)

    def test_a_model_written_name_finds_the_right_row_whatever_the_row_says(self):
        self.assertEqual(self.api(self.LIVE).country_id("Cook Islands"), 3)
        self.assertEqual(self.api(self.LIVE).country_id("Fiji"), 1)

    def test_rows_with_no_code_are_still_found_by_name(self):
        rows = [{"id": 9, "short_name": "ATL", "long_name": "Atlantis"}]
        self.assertEqual(self.api(rows).country_id("atlantis"), 9)
        self.assertIsNone(self.api(rows).country_id("Narnia"))

    def test_the_runners_name_table_agrees_with_the_one_in_country_codes(self):
        # The script runs without Django, so it keeps its own copy of the name -> code table
        from country.codes import _BY_LONG_NAME

        self.assertEqual(self.runner.COUNTRY_NAME_CODES, _BY_LONG_NAME)
