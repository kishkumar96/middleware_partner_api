import json

from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework import status
from rest_framework.test import APITestCase

from country.models import Country

from .models import RiskInformation, RiskScenario

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
