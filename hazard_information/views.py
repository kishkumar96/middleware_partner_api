from rest_framework.generics import ListAPIView, RetrieveAPIView
from rest_framework.permissions import AllowAny

from .models import HazardInformation
from .serializers import HazardInformationSerializer


class HazardInformationListView(ListAPIView):
    permission_classes = [AllowAny]
    queryset = HazardInformation.objects.all().order_by("-id")
    serializer_class = HazardInformationSerializer
    filterset_fields = (
        "country",
        "hazard_type",
        "event_type",
        "hazard_source",
        "slr_scenario_m",
        "return_period_years",
        "event_label",
        "model_run",
    )


class HazardInformationRetrieveView(RetrieveAPIView):
    permission_classes = [AllowAny]
    serializer_class = HazardInformationSerializer
    queryset = HazardInformation.objects.all().select_related(
        "country",
        "hazard_type",
        "event_type",
    )
