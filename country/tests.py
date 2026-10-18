from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.migrations.executor import MigrationExecutor
from django.db import connection
from django.test import TestCase, TransactionTestCase
from rest_framework.test import APITestCase

from .codes import code_for, normalise_code
from .lookup import find_country
from .models import Country


class CodeHelperTests(TestCase):
    def test_the_names_seen_on_the_live_api_resolve_to_one_code_each(self):
        # Live: 1 FJI/FIJI, 2 VU/Vanuatu, 3 CK/"Cook Island", 4 Ton/Tonga, 5 WS/Samoa
        self.assertEqual(code_for("FJI", "FIJI"), "FJ")
        self.assertEqual(code_for("VU", "Vanuatu"), "VU")
        self.assertEqual(code_for("CK", "Cook Island"), "CK")
        self.assertEqual(code_for("Ton", "Tonga"), "TO")
        self.assertEqual(code_for("WS", "Samoa"), "WS")
        self.assertEqual(code_for("CK", "Cook Islands"), "CK")  # as the seed migration names it

    def test_either_name_is_enough(self):
        self.assertEqual(code_for("Ton", ""), "TO")
        self.assertEqual(code_for("", "Samoa"), "WS")
        self.assertEqual(code_for("  ton ", None), "TO")

    def test_an_unrecognised_country_gets_no_code(self):
        self.assertIsNone(code_for("ATL", "Atlantis"))
        self.assertIsNone(code_for("", ""))
        self.assertIsNone(code_for(None, None))

    def test_normalise_code(self):
        self.assertEqual(normalise_code(" ck "), "CK")
        self.assertIsNone(normalise_code(""))
        self.assertIsNone(normalise_code("   "))
        self.assertIsNone(normalise_code(None))


class CountryCodeModelTests(TestCase):
    def setUp(self):
        # Migration 0003 seeds the Cook Islands (and 0005 gives it the code CK); start from none
        Country.objects.all().delete()

    def make(self, short, code=None, long="Somewhere"):
        return Country.objects.create(short_name=short, long_name=long, code=code)

    def test_a_code_is_stored_upper_case_and_trimmed(self):
        self.assertEqual(self.make("a", " ck ").code, "CK")

    def test_a_blank_code_is_stored_as_null_so_countries_without_one_do_not_clash(self):
        first, second = self.make("a", ""), self.make("b", "")
        self.assertIsNone(Country.objects.get(pk=first.pk).code)
        self.assertIsNone(Country.objects.get(pk=second.pk).code)

    def test_two_countries_cannot_share_a_code(self):
        self.make("a", "CK")
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.make("b", "ck")

    def test_a_code_must_be_two_letters(self):
        for bad in ("C", "CKK", "C1", "1K", "C-"):
            country = Country(short_name="x", long_name="x", code=bad)
            with self.assertRaises(ValidationError, msg=bad):
                country.full_clean()

    def test_a_country_may_have_no_code_at_all(self):
        country = Country(short_name="x", long_name="x")
        country.full_clean()  # does not raise


class CountryCodeApiTests(APITestCase):
    def setUp(self):
        Country.objects.all().delete()  # the seeded Cook Islands row
        self.ck = Country.objects.create(short_name="CK", long_name="Cook Islands", code="CK")
        self.to = Country.objects.create(short_name="Ton", long_name="Tonga", code="TO")
        self.unset = Country.objects.create(short_name="ATL", long_name="Atlantis")

    def rows(self, query=""):
        response = self.client.get(f"/partner_api/v1/country/{query}")
        self.assertEqual(response.status_code, 200)
        return response.json()

    def test_every_country_reports_its_code_or_null(self):
        by_short = {row["short_name"]: row for row in self.rows()}
        self.assertEqual(by_short["CK"]["code"], "CK")
        self.assertEqual(by_short["Ton"]["code"], "TO")
        self.assertIsNone(by_short["ATL"]["code"])

    def test_a_country_can_be_found_by_code_whatever_its_id(self):
        self.assertEqual([row["id"] for row in self.rows("?code=TO")], [self.to.pk])
        self.assertEqual([row["id"] for row in self.rows("?code=ck")], [self.ck.pk])  # any case

    def test_an_unknown_code_finds_nothing_not_everything(self):
        self.assertEqual(self.rows("?code=XX"), [])

    def test_no_code_filter_lists_every_country(self):
        self.assertEqual(len(self.rows()), 3)
        self.assertEqual(len(self.rows("?code=")), 3)

    def test_one_country_reports_its_code(self):
        response = self.client.get(f"/partner_api/v1/country/{self.to.pk}/")
        self.assertEqual(response.json()["code"], "TO")


class PopulateCodesMigrationTests(TransactionTestCase):
    """The data migration gives existing rows their code, and cannot fail on a clash."""

    migrate_from = [("country", "0004_country_code")]
    migrate_to = [("country", "0005_populate_country_code")]

    def setUp(self):
        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_from)
        apps = executor.loader.project_state(self.migrate_from).apps
        self.Country = apps.get_model("country", "Country")
        self.Country.objects.all().delete()

    def run_migration(self):
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(self.migrate_to)
        return executor.loader.project_state(self.migrate_to).apps.get_model("country", "Country")

    def tearDown(self):
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(executor.loader.graph.leaf_nodes())

    def test_live_style_rows_get_their_codes_and_unknown_rows_stay_blank(self):
        for short, long in (("FJI", "FIJI"), ("VU", "Vanuatu"), ("CK", "Cook Island"), ("Ton", "Tonga"), ("WS", "Samoa"), ("ATL", "Atlantis")):
            self.Country.objects.create(short_name=short, long_name=long)
        Country = self.run_migration()
        codes = {c.short_name: c.code for c in Country.objects.all()}
        self.assertEqual(codes, {"FJI": "FJ", "VU": "VU", "CK": "CK", "Ton": "TO", "WS": "WS", "ATL": None})

    def test_two_rows_for_one_country_do_not_fail_the_migration(self):
        first = self.Country.objects.create(short_name="CK", long_name="Cook Island")
        second = self.Country.objects.create(short_name="CK", long_name="Cook Islands")
        Country = self.run_migration()
        self.assertEqual(Country.objects.get(pk=first.pk).code, "CK")
        self.assertIsNone(Country.objects.get(pk=second.pk).code)  # left for an operator

    def test_a_code_already_set_by_hand_is_kept(self):
        self.Country.objects.create(short_name="odd", long_name="Odd", code="ZZ")
        self.Country.objects.create(short_name="VU", long_name="Vanuatu")
        Country = self.run_migration()
        self.assertEqual(Country.objects.get(short_name="odd").code, "ZZ")
        self.assertEqual(Country.objects.get(short_name="VU").code, "VU")


class SeededCookIslandsTests(TestCase):
    def test_the_cook_islands_seeded_by_migration_have_their_code(self):
        self.assertEqual(Country.objects.get(short_name="CK").code, "CK")


class FindCountryTests(TestCase):
    def setUp(self):
        Country.objects.all().delete()  # the seeded Cook Islands row
        # As the live API has them: names that differ from what a model or a person writes
        self.fiji = Country.objects.create(short_name="FJI", long_name="FIJI", code="FJ")
        self.ck = Country.objects.create(short_name="CK", long_name="Cook Island", code="CK")
        self.to = Country.objects.create(short_name="Ton", long_name="Tonga", code="TO")
        self.uncoded = Country.objects.create(short_name="ATL", long_name="Atlantis")

    def test_a_code_finds_its_country_in_any_case(self):
        self.assertEqual(find_country("CK"), self.ck)
        self.assertEqual(find_country(" to "), self.to)

    def test_a_name_as_a_model_writes_it_finds_the_right_row_whatever_the_row_says(self):
        # RiskScape's table says "Cook Islands"; the row says "Cook Island"
        self.assertEqual(find_country("Cook Islands"), self.ck)
        self.assertEqual(find_country("cook island"), self.ck)
        self.assertEqual(find_country("Fiji"), self.fiji)
        self.assertEqual(find_country("Ton"), self.to)

    def test_a_country_with_no_code_is_still_found_by_its_own_names(self):
        self.assertEqual(find_country("ATL"), self.uncoded)
        self.assertEqual(find_country("atlantis"), self.uncoded)

    def test_the_code_wins_over_a_row_that_merely_has_that_text_as_a_name(self):
        impostor = Country.objects.create(short_name="TO", long_name="Not Tonga")
        self.assertEqual(find_country("TO"), self.to)
        self.assertNotEqual(find_country("TO"), impostor)

    def test_an_unknown_or_blank_identifier_finds_nothing(self):
        for value in ("Narnia", "XX", "", "   ", None):
            self.assertIsNone(find_country(value), msg=repr(value))
