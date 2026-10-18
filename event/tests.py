from rest_framework.test import APITestCase

from country.models import Country
from cyclone_track.models import CycloneTrack
from event_type.models import EventType

from .models import Event


class EventSerializerIdentityTests(APITestCase):
    def test_event_exposes_id_and_cyclone_name(self):
        country = Country.objects.create(short_name="VU", long_name="Vanuatu")
        track = CycloneTrack.objects.create(cyclone_name="LOLA", country=country, track_file="t.csv")
        event_type = EventType.objects.create(event_name="Tropical Cyclone")
        event = Event.objects.create(
            title="Tropical Cyclone Lola", event_type=event_type, country=country, cyclone_track=track
        )
        response = self.client.get("/partner_api/v1/event/")
        row = next(r for r in response.data["results"] if r["id"] == event.pk) if "results" in response.data else next(
            r for r in response.data if r["id"] == event.pk
        )
        self.assertEqual(row["cyclone_name"], "LOLA")


class NonCycloneEventTests(APITestCase):
    """Events of other types (flood, earthquake, ...) have no cyclone track."""

    def test_event_without_a_track_is_listed_with_its_type(self):
        country = Country.objects.create(short_name="TO", long_name="Tonga")
        event_type = EventType.objects.create(event_name="Earthquake")
        event = Event.objects.create(title="Quake", event_type=event_type, country=country)
        self.assertIsNone(event.cyclone_track)

        response = self.client.get("/partner_api/v1/event/")
        results = response.data["results"] if "results" in response.data else response.data
        row = next(r for r in results if r["id"] == event.pk)
        self.assertEqual(row["event_type_name"], "Earthquake")
        self.assertIsNone(row["cyclone_track"])
        self.assertIsNone(row["cyclone_name"])
        self.assertIsNone(row["track_issued_time"])
