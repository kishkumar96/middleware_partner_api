from rest_framework.generics import ListAPIView, ListCreateAPIView, RetrieveAPIView
from rest_framework.pagination import PageNumberPagination
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.permissions import AllowAny, IsAuthenticatedOrReadOnly

from .models import RiskInformation, RiskScenario
from .serializers import RiskInformationSerializer, RiskScenarioSerializer


class RiskInformationPagination(PageNumberPagination):
    page_size = 25
    page_size_query_param = "page_size"
    max_page_size = 200


class RiskInformationListCreateView(ListCreateAPIView):
    queryset = RiskInformation.objects.all().order_by("-id")
    serializer_class = RiskInformationSerializer
    pagination_class = RiskInformationPagination

    # GET is public, POST requires auth
    permission_classes = (IsAuthenticatedOrReadOnly,)

    # Needed for file upload
    parser_classes = (MultiPartParser, FormParser)


class RiskInformationRetrieveView(RetrieveAPIView):
    queryset = RiskInformation.objects.all().select_related(
        "risk_category",
        "country",
        "event_type",
    )
    serializer_class = RiskInformationSerializer

    # GET is public (same as list), POST would be in list/create
    permission_classes = (IsAuthenticatedOrReadOnly,)


class RiskScenarioListView(ListAPIView):
    permission_classes = [AllowAny]
    queryset = RiskScenario.objects.all().select_related("risk_information").order_by("-id")
    serializer_class = RiskScenarioSerializer
    pagination_class = RiskInformationPagination
    filterset_fields = (
        "risk_information",
        "risk_information__country",
        "slr_scenario_m",
        "return_period_years",
        "event_label",
        "is_annual_average_loss",
    )


class RiskScenarioRetrieveView(RetrieveAPIView):
    permission_classes = [AllowAny]
    queryset = RiskScenario.objects.all().select_related("risk_information")
    serializer_class = RiskScenarioSerializer
