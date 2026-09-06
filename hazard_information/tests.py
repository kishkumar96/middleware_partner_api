from rest_framework import status
from rest_framework.test import APITestCase

from country.models import Country

from .models import HazardInformation


class HazardInformationCountryFilterTests(APITestCase):
    """Same regression class as risk_information's country-filter bug
    (d007985): the list view must declare "country" in filterset_fields,
    or ?country= is silently ignored and every request returns every
    country's rows. HazardInformationListView already does this
    correctly (see the code comment in risk_information/views.py) -- this
    guards against that ever regressing."""

    def setUp(self):
        self.ck = Country.objects.create(short_name="CK", long_name="Cook Islands")
        self.vu = Country.objects.create(short_name="VU", long_name="Vanuatu")
        self.ck_row = HazardInformation.objects.create(
            title="CK combined_slr0.25_rp100",
            protocol="xyz",
            url="https://example.spc.int/hazard_tiles/combined/0.25/100/{z}/{x}/{y}.png",
            country=self.ck,
        )
        self.vu_row = HazardInformation.objects.create(
            title="VU TC Harold wind",
            protocol="WMS",
            url="https://gemthreddshpc.spc.int/thredds/wms/vu_hazard/TC/Harold/_merged.nc",
            country=self.vu,
        )
        self.list_url = "/partner_api/v1/hazard_information/"

    def test_country_filter_returns_only_that_countrys_rows(self):
        response = self.client.get(self.list_url, {"country": self.ck.pk})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        ids = [row["id"] for row in response.data]
        self.assertEqual(ids, [self.ck_row.pk])

    def test_different_country_filter_returns_only_its_own_rows(self):
        response = self.client.get(self.list_url, {"country": self.vu.pk})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        ids = [row["id"] for row in response.data]
        self.assertEqual(ids, [self.vu_row.pk])

    def test_no_country_filter_returns_all_countries(self):
        response = self.client.get(self.list_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        ids = {row["id"] for row in response.data}
        self.assertEqual(ids, {self.ck_row.pk, self.vu_row.pk})
