"""
URL configuration for middleware_partner_api project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/6.0/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""

from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import path
from . import views
from rest_framework.authtoken.views import obtain_auth_token

from .admin_site import partner_admin_site


admin.site.site_header = "PaRTnR2 API"
admin.site.site_title = "PaRTnR2 API"
admin.site.index_title = "PaRTnR2 API"

from country.views import CountryListView, CountryRetrieveView
from django.urls import include
from cyclone_track.views import (
    CycloneTrackListCreateView,
    CycloneTrackModelInputView,
    CycloneTrackRetrieveView,
)
from risk_information.views import (
    RiskInformationListCreateView,
    RiskInformationRetrieveView,
    RiskScenarioListView,
    RiskScenarioBreakdownListView,
    RiskScenarioRetrieveView,
)
from hazard_information.views import HazardInformationListView, HazardInformationRetrieveView
from hazard_tiles.views import hazard_tile
from citizen_science.views import CitizenScienceListCreateView
from event.views import EventListCreateView, EventRetrieveView
from model.views import ModelRunListView
from model.views_update import ModelRunUpdateCompletedView
from model.views_logs import ModelLogsListCreateView
from model.views_queue import (
    ModelOperationsStatusView,
    ModelRunClaimView,
    ModelRunFinishView,
    RunnerHeartbeatView,
)
from model.views_operator import (
    OperatorMeView,
    OperatorPipelinesView,
    OperatorRunsView,
    OperatorSubmitTrackView,
)
from mailer.views import (
    MailConfigurationDetailView,
    MailConfigurationListCreateView,
    MailConfigurationSendView,
    MailRecipientDetailView,
    MailRecipientListCreateView,
)



urlpatterns = [
    path("", views.root),
    path("partner_api/", views.partner_api_root),
    path("partner_api/admin/", partner_admin_site.urls),
    path("partner_api/api-token-auth/", obtain_auth_token),
    path("partner_api/v1/country/", CountryListView.as_view()),
    path("partner_api/v1/country/<int:pk>", CountryRetrieveView.as_view()),
    path("partner_api/v1/country/<int:pk>/", CountryRetrieveView.as_view()),
    path("partner_api/v1/cyclone_track/", CycloneTrackListCreateView.as_view()),
    path("partner_api/v1/cyclone_track/<int:pk>", CycloneTrackRetrieveView.as_view()),
    path("partner_api/v1/cyclone_track/<int:pk>/", CycloneTrackRetrieveView.as_view()),
    path("partner_api/v1/cyclone_track/<int:pk>/model_input/", CycloneTrackModelInputView.as_view()),
    path("partner_api/v1/risk_information/", RiskInformationListCreateView.as_view()),
    path("partner_api/v1/risk_information/<int:pk>", RiskInformationRetrieveView.as_view()),
    path("partner_api/v1/risk_information/<int:pk>/", RiskInformationRetrieveView.as_view()),
    path("partner_api/v1/risk_scenario/", RiskScenarioListView.as_view()),
    path("partner_api/v1/risk_scenario/<int:pk>", RiskScenarioRetrieveView.as_view()),
    path("partner_api/v1/risk_scenario/<int:pk>/", RiskScenarioRetrieveView.as_view()),
    path("partner_api/v1/risk_scenario_breakdown/", RiskScenarioBreakdownListView.as_view()),
    path("partner_api/v1/hazard_information/", HazardInformationListView.as_view()),
    path(
        "partner_api/v1/hazard_information/<int:pk>",
        HazardInformationRetrieveView.as_view(),
    ),
    path(
        "partner_api/v1/hazard_information/<int:pk>/",
        HazardInformationRetrieveView.as_view(),
    ),
    path(
        "partner_api/v1/hazard_tiles/<str:source>/<str:slr>/<str:selector>/<int:z>/<int:x>/<int:y>.png",
        hazard_tile,
    ),
    path("partner_api/v1/citizen_science/", CitizenScienceListCreateView.as_view()),
    path("partner_api/v1/event/", EventListCreateView.as_view()),
    path("partner_api/v1/event/<int:pk>", EventRetrieveView.as_view()),
    path("partner_api/v1/event/<int:pk>/", EventRetrieveView.as_view()),

    # ModelRun
    path("partner_api/v1/model_run/", ModelRunListView.as_view()),
    # Runner queue: claim the next job for a server, then report its result
    path("partner_api/v1/model_run/claim/", ModelRunClaimView.as_view()),
    # Operator page: superusers submit a track by hand and follow its runs
    path("partner_api/v1/operator/me/", OperatorMeView.as_view()),
    path("partner_api/v1/operator/pipelines/", OperatorPipelinesView.as_view()),
    path("partner_api/v1/operator/tracks/", OperatorSubmitTrackView.as_view()),
    path("partner_api/v1/operator/runs/", OperatorRunsView.as_view()),
    path("partner_api/v1/model_run/<int:pk>/finish/", ModelRunFinishView.as_view()),
    path("partner_api/v1/model_run/operations/", ModelOperationsStatusView.as_view()),
    path("partner_api/v1/model_run/heartbeat/", RunnerHeartbeatView.as_view()),
        # PATCH model_run_completed
        path("partner_api/v1/model_run/<int:pk>/update_completed/", ModelRunUpdateCompletedView.as_view()),

        # ModelLogs
        path("partner_api/v1/model_run_logs/", ModelLogsListCreateView.as_view()),

    # Mailer
    path("partner_api/v1/mailer/recipients/", MailRecipientListCreateView.as_view()),
    path("partner_api/v1/mailer/recipients/<int:pk>/", MailRecipientDetailView.as_view()),
    path("partner_api/v1/mailer/configurations/", MailConfigurationListCreateView.as_view()),
    path("partner_api/v1/mailer/configurations/<int:pk>/", MailConfigurationDetailView.as_view()),
    path("partner_api/v1/mailer/configurations/<int:pk>/send/", MailConfigurationSendView.as_view()),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
