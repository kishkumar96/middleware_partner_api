import csv
import io
import json
import os
import tempfile

from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from rest_framework import status
from rest_framework.test import APITestCase

from country.models import Country

from .models import RiskInformation, RiskScenario, RiskScenarioBreakdown

MINIMAL_GEOJSON = json.dumps(
    {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {},
                "geometry": {"type": "Point", "coordinates": [0, 0]},
            }
        ],
    }
).encode("utf-8")


def make_upload(name: str) -> SimpleUploadedFile:
    return SimpleUploadedFile(name, MINIMAL_GEOJSON, content_type="application/geo+json")


class RiskInformationCountryFilterTests(APITestCase):
    """Regression test for the bug fixed in d007985 (fix(risk-information):
    filter risk_information by country server-side): the list view never
    declared filterset_fields, so DjangoFilterBackend silently dropped
    ?country= and every request returned all countries' rows -- ~68MB
    regardless of which country asked. Guards against that ever coming
    back, e.g. via a refactor that drops filterset_fields again."""

    def setUp(self):
        self.ck = Country.objects.create(short_name="CK", long_name="Cook Islands")
        self.vu = Country.objects.create(short_name="VU", long_name="Vanuatu")
        self.ck_row = RiskInformation.objects.create(
            title="CK national summary",
            country=self.ck,
            upload=make_upload("ck_summary.geojson"),
        )
        self.vu_row = RiskInformation.objects.create(
            title="VU national summary",
            country=self.vu,
            upload=make_upload("vu_summary.geojson"),
        )
        self.list_url = "/partner_api/v1/risk_information/"

    def test_country_filter_returns_only_that_countrys_rows(self):
        response = self.client.get(self.list_url, {"country": self.ck.pk})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        ids = [row["id"] for row in response.data["results"]]
        self.assertEqual(ids, [self.ck_row.pk])

    def test_different_country_filter_returns_only_its_own_rows(self):
        response = self.client.get(self.list_url, {"country": self.vu.pk})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        ids = [row["id"] for row in response.data["results"]]
        self.assertEqual(ids, [self.vu_row.pk])

    def test_no_country_filter_returns_all_countries(self):
        response = self.client.get(self.list_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        ids = {row["id"] for row in response.data["results"]}
        self.assertEqual(ids, {self.ck_row.pk, self.vu_row.pk})


class RiskInformationDeferLargeAssetsTests(APITestCase):
    """Regression test for 9321411 (perf(risk-information): defer
    damaged-buildings/roads geometry until toggled on): ?defer_large_assets=true
    must null out geometry for damaged-building(s)/damaged-road(s) rows only,
    leave every other row's geometry untouched, and never apply to the
    retrieve-by-id endpoint (which the frontend uses to fetch a deferred
    row's full geometry once its overlay is switched on)."""

    def setUp(self):
        self.country = Country.objects.create(short_name="VU", long_name="Vanuatu")
        self.buildings_row = RiskInformation.objects.create(
            title="Damaged Buildings",
            country=self.country,
            upload=make_upload("vu_damaged-buildings.geojson"),
        )
        self.summary_row = RiskInformation.objects.create(
            title="National summary",
            country=self.country,
            upload=make_upload("vu_national_summary.geojson"),
        )
        self.list_url = "/partner_api/v1/risk_information/"

    def _geometry_by_id(self, response):
        return {row["id"]: row["geometry"] for row in response.data["results"]}

    def test_defer_large_assets_nulls_only_damaged_buildings_geometry(self):
        response = self.client.get(
            self.list_url, {"country": self.country.pk, "defer_large_assets": "true"}
        )
        geometry_by_id = self._geometry_by_id(response)
        self.assertIsNone(geometry_by_id[self.buildings_row.pk])
        self.assertIsNotNone(geometry_by_id[self.summary_row.pk])

    def test_defer_large_assets_nulls_ref_floodplain_exposure_geometry(self):
        floodplain_row = RiskInformation.objects.create(
            title="floodplain exposure (REF)",
            country=self.country,
            upload=make_upload("floodplain-exposure.geojson"),
        )
        response = self.client.get(
            self.list_url, {"country": self.country.pk, "defer_large_assets": "true"}
        )
        self.assertIsNone(self._geometry_by_id(response)[floodplain_row.pk])

    def test_without_the_flag_all_geometry_is_present(self):
        response = self.client.get(self.list_url, {"country": self.country.pk})
        geometry_by_id = self._geometry_by_id(response)
        self.assertIsNotNone(geometry_by_id[self.buildings_row.pk])
        self.assertIsNotNone(geometry_by_id[self.summary_row.pk])

    def test_retrieve_endpoint_ignores_defer_large_assets(self):
        retrieve_url = f"/partner_api/v1/risk_information/{self.buildings_row.pk}/"
        response = self.client.get(retrieve_url, {"defer_large_assets": "true"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIsNotNone(response.data["geometry"])


class RiskScenarioCountryFilterTests(APITestCase):
    """RiskScenario is denormalized so a client can filter by
    risk_information__country directly (CK's dimensional SLR x
    return-period catalog). Same regression class as RiskInformation's
    country filter -- guards against filterset_fields silently dropping it."""

    def setUp(self):
        self.ck = Country.objects.create(short_name="CK", long_name="Cook Islands")
        self.vu = Country.objects.create(short_name="VU", long_name="Vanuatu")
        ck_risk = RiskInformation.objects.create(
            title="CK combined probabilistic",
            country=self.ck,
            upload=make_upload("ck_combined.geojson"),
        )
        vu_risk = RiskInformation.objects.create(
            title="VU national summary",
            country=self.vu,
            upload=make_upload("vu_summary.geojson"),
        )
        self.ck_scenario = RiskScenario.objects.create(
            risk_information=ck_risk,
            slr_scenario_m=0.25,
            return_period_years=100,
        )
        RiskScenario.objects.create(
            risk_information=vu_risk,
            slr_scenario_m=0.25,
            return_period_years=100,
        )
        self.list_url = "/partner_api/v1/risk_scenario/"

    def test_filter_by_risk_information_country(self):
        response = self.client.get(self.list_url, {"risk_information__country": self.ck.pk})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        ids = [row["id"] for row in response.data["results"]]
        self.assertEqual(ids, [self.ck_scenario.pk])


# Two scenarios (SLR 0, return periods 10 and 100) of a probabilistic
# product, each with two asset types in two sectors. Losses per asset type:
#   RP10:  house 100 (building), wharf 50 (infrastructure)  -> total 150
#   RP100: house 400 (building), wharf 200 (infrastructure) -> total 600
ASSETS = {
    10: [("House", "Build-Residential", 100, 0), ("Wharf", "Infra-Ports", 0, 50)],
    100: [("House", "Build-Residential", 400, 0), ("Wharf", "Infra-Ports", 0, 200)],
}


def national_row(rp, building, infra):
    return {
        "SLR": "0", "Return_Period": str(rp), "Exposed.Population": "10",
        "Loss.Total": building + infra, "Loss.Building_Loss": building, "Loss.Road_Loss": 0,
        "Loss.Infrastructure_Loss": infra, "Loss.Crop_Loss": 0,
    }


class ImportCkRiskScenariosTests(APITestCase):
    def setUp(self):
        # Seeded by a country data migration; the importer takes the first CK row
        self.ck = Country.objects.filter(short_name="CK").first() or Country.objects.create(
            short_name="CK", long_name="Cook Islands"
        )
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = os.path.join(self.tmp.name, "Prob_TC_AAL")
        os.makedirs(self.folder)
        self.write("impact-asset-type.csv", [
            {"UseType": name, "SLR": "0.0", "Return_Period": rp, "Sector": sector, "Exposed_Count": 1,
             "Total_Loss.Total": b + i, "Total_Loss.Building_Loss": b, "Total_Loss.Road_Loss": 0,
             "Total_Loss.Infrastructure_Loss": i, "Total_Loss.Crop_Loss": 0, "Average_Inundation_Depth": 0.5}
            for rp, assets in ASSETS.items() for name, sector, b, i in assets
        ])
        self.write("impact-asset-sector.csv", [
            {"Sector": sector, "SLR": "0.0", "Return_Period": rp, "Total_Loss.Total": b + i}
            for rp, assets in ASSETS.items() for _, sector, b, i in assets
        ])
        self.write("national-average-loss.csv", [{"SLR": "0", "AAL_Total": 30}])
        self.write("aal_by_sector.csv", [
            {"Sector": "Build-Residential", "SLR": "0", "AAL": 20},
            {"Sector": "Infra-Ports", "SLR": "0", "AAL": 10},
        ])

    def write(self, name, rows):
        with open(os.path.join(self.folder, name), "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)

    def run_import(self):
        err = io.StringIO()
        call_command("import_ck_risk_scenarios", data_dir=self.tmp.name, stdout=io.StringIO(), stderr=err)
        return err.getvalue()

    def scenario(self, rp):
        return RiskScenario.objects.get(return_period_years=rp, is_annual_average_loss=False)

    def test_imports_scenarios_with_breakdowns(self):
        self.write("national-impact.csv", [national_row(10, 100, 50), national_row(100, 400, 200)])
        err = self.run_import()
        self.assertNotIn("relabelled", err)

        rp10 = self.scenario(10)
        self.assertEqual(rp10.total_loss, 150)
        assets = rp10.breakdowns.filter(dimension=RiskScenarioBreakdown.ASSET_TYPE)
        self.assertEqual({(b.name, b.sector, b.total_loss) for b in assets},
                         {("House", "Build-Residential", 100), ("Wharf", "Infra-Ports", 50)})
        self.assertEqual(rp10.breakdowns.filter(dimension=RiskScenarioBreakdown.SECTOR).count(), 2)
        aal = RiskScenario.objects.get(is_annual_average_loss=True)
        self.assertEqual(sorted(aal.breakdowns.values_list("total_loss", flat=True)), [10, 20])

    def test_rerun_replaces_breakdowns_instead_of_duplicating(self):
        self.write("national-impact.csv", [national_row(10, 100, 50), national_row(100, 400, 200)])
        self.run_import()
        self.run_import()
        self.assertEqual(RiskScenario.objects.count(), 3)
        self.assertEqual(RiskScenarioBreakdown.objects.count(), 4 + 4 + 2)

    def test_swapped_national_labels_are_relabelled_to_matching_breakdown(self):
        # The Prob_combo_AAL defect: each national row filed under the other's key
        self.write("national-impact.csv", [national_row(10, 400, 200), national_row(100, 100, 50)])
        err = self.run_import()
        self.assertIn("2 national-impact.csv row(s) were under the wrong scenario label", err)
        self.assertEqual(self.scenario(10).total_loss, 150)
        self.assertEqual(self.scenario(100).total_loss, 600)

    def test_rerun_corrects_previously_imported_values(self):
        self.write("national-impact.csv", [national_row(10, 400, 200), national_row(100, 100, 50)])
        info = RiskInformation.objects.create(
            title="CK tc national impact + AAL",
            country=self.ck,
            upload=SimpleUploadedFile("x.txt", b"x"),
        )
        RiskScenario.objects.create(risk_information=info, slr_scenario_m=0, return_period_years=10, total_loss=600)
        self.run_import()
        self.assertEqual(self.scenario(10).total_loss, 150)

    def test_ambiguous_mismatch_is_imported_as_is_with_warning(self):
        # National totals that match no breakdown: nothing to relabel to
        self.write("national-impact.csv", [national_row(10, 999, 0), national_row(100, 400, 200)])
        err = self.run_import()
        self.assertIn("don't match their sector/asset breakdown", err)
        self.assertEqual(self.scenario(10).total_loss, 999)


class RiskScenarioBreakdownApiTests(APITestCase):
    def setUp(self):
        ck = Country.objects.create(short_name="CK", long_name="Cook Islands")
        info = RiskInformation.objects.create(title="CK tc", country=ck, upload=make_upload("tc.geojson"))
        self.rp10 = RiskScenario.objects.create(risk_information=info, slr_scenario_m=0, return_period_years=10)
        rp100 = RiskScenario.objects.create(risk_information=info, slr_scenario_m=0, return_period_years=100)
        for scenario, loss in ((self.rp10, 5), (rp100, 50)):
            RiskScenarioBreakdown.objects.create(risk_scenario=scenario, dimension="sector", name="Ports", total_loss=loss)
            RiskScenarioBreakdown.objects.create(
                risk_scenario=scenario, dimension="asset_type", name="Wharf", sector="Ports", total_loss=loss
            )

    def test_filters_by_scenario_and_dimension(self):
        response = self.client.get(
            "/partner_api/v1/risk_scenario_breakdown/", {"risk_scenario": self.rp10.pk, "dimension": "asset_type"}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            [(r["name"], r["sector"], r["total_loss"]) for r in response.data["results"]], [("Wharf", "Ports", 5)]
        )
