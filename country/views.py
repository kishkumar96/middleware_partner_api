from rest_framework.views import APIView
from rest_framework.generics import RetrieveAPIView
from rest_framework.response import Response
from rest_framework.permissions import AllowAny
from .codes import normalise_code
from .models import Country
from .serializers import CountrySerializer

class CountryListView(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        countries = Country.objects.all().prefetch_related("islands", "regions")
        # ?code=CK finds a country by its canonical code (case-insensitive), whatever its id here
        code = normalise_code(request.query_params.get("code"))
        if code:
            countries = countries.filter(code=code)
        serializer = CountrySerializer(countries, many=True)
        return Response(serializer.data)


class CountryRetrieveView(RetrieveAPIView):
    permission_classes = [AllowAny]
    serializer_class = CountrySerializer

    def get_queryset(self):
        return Country.objects.all().prefetch_related("islands", "regions")
