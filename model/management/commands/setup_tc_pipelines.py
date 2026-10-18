"""Configure the two cyclone impact pipelines: Models, their chain and triggers.

  rapid  Official forecast track -> RiskScape platform model (runner
         `--backend platform --server-name riskscape.nz`). The model fetches
         the track itself from the Partner API, given its URL.
  pdna   Post-event best track -> on-prem chain on lotgemdev01 (runner
         `--backend local --server-name lotgemdev01`):
         QuickSurge (storm surge) -> Wind Hazard (TCRM) -> RiskScape impact.
         The hazard models fetch the track from the same Partner API; each
         docker step gets it as CYCLONE_TRACK_URL / CYCLONE_TRACK_ID.

The on-prem commands are the ones registered in the 27 March 2026 production
database, with `-ti` dropped (a runner has no terminal) and the track
variables added. Safe to re-run: everything is matched by name and updated.
"""

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from cyclone_track_type.models import CycloneTrackType
from model.models import Model, ModelDomain, ModelPriority, ModelTrigger

TRACK_ENV = "-e CYCLONE_TRACK_URL={track_url} -e CYCLONE_TRACK_ID={track_id}"

PDNA_CHAIN = [
    {
        "name": "QuickSurge",
        "location": "/mnt/DATA/production/QuickSurge",
        "run_command": (
            "docker run --rm -v /mnt/DATA/production/QuickSurge:/QuickSurge "
            f'--cpuset-cpus=0-21 {TRACK_ENV} quickly /bin/bash -c ". /opt/conda/bin/activate; '
            'cd /QuickSurge/Python_Codes; python main_IDA_PDNA_StormSurge.py"'
        ),
    },
    {
        "name": "Wind Hazard",
        "location": "/mnt/DATA/production/TCHA",
        "run_command": (
            "docker run --rm -v /mnt/DATA/production/TCHA:/TCHA "
            f'--cpuset-cpus=16-17 {TRACK_ENV} tcha-prod /bin/bash -c ". /opt/miniconda/bin/activate; '
            'cd /TCHA/tcrm; python main.py"'
        ),
    },
    {
        "name": "RiskScape",
        "location": "/mnt/DATA/production/rs-projects",
        # Outputs must land in {output_dir}: that's what the runner uploads.
        # Check the flag against the RiskScape CLI version on the server.
        "run_command": "riskscape model run Cyclone-PDIE --output {output_dir}",
    },
]


class Command(BaseCommand):
    help = "Create or update the rapid (forecast) and PDNA (best track) cyclone model pipelines."

    def add_arguments(self, parser):
        parser.add_argument("--rapid-project", default="niwa/partner/rsmc-tcwc",
                            help="RiskScape platform project full slug")
        parser.add_argument("--rapid-model", default="Rapid-Exposure-Forecast-GeoJSON", help="RiskScape platform model id")
        parser.add_argument("--rapid-param", default="hazard",
                            help="that model's parameter for the track URL")
        parser.add_argument("--onprem-server", default="lotgemdev01",
                            help="server_name the on-prem runner uses")
        parser.add_argument("--create-track-types", action="store_true",
                            help="create the forecast / best track types if missing")

    def track_type(self, keyword, default_name, create):
        match = CycloneTrackType.objects.filter(name__icontains=keyword).order_by("id").first()
        if match:
            return match
        if not create:
            existing = ", ".join(CycloneTrackType.objects.values_list("name", flat=True)) or "none"
            raise CommandError(
                f"No cyclone track type with '{keyword}' in its name (existing: {existing}). "
                "Create it in the admin, or re-run with --create-track-types."
            )
        return CycloneTrackType.objects.create(name=default_name)

    @transaction.atomic
    def handle(self, *args, **options):
        forecast = self.track_type("forecast", "Official Forecast Track", options["create_track_types"])
        best = self.track_type("best", "Post-event Best Track", options["create_track_types"])
        domain, _ = ModelDomain.objects.get_or_create(name="Tropical cyclone")
        high, _ = ModelPriority.objects.get_or_create(name="High")
        medium, _ = ModelPriority.objects.get_or_create(name="Medium")

        rapid, _ = Model.objects.update_or_create(
            name=options["rapid_model"],
            server_name="riskscape.nz",
            defaults={
                "location": options["rapid_project"],
                # The track as the model can read it (ISO-UTC times); the model's `hazard`
                # input takes the URL as it is, not as a quoted RiskScape expression
                "inputs": {options["rapid_param"]: "{model_track_url}"},
                "run_command": "",
                "next_model": None,
            },
        )

        chain = []
        for spec in PDNA_CHAIN:
            model, _ = Model.objects.update_or_create(
                name=spec["name"],
                server_name=options["onprem_server"],
                defaults={"location": spec["location"], "run_command": spec["run_command"], "inputs": {}},
            )
            chain.append(model)
        for model, after in zip(chain, chain[1:] + [None]):
            model.next_model = after
            model.save(update_fields=["next_model"])

        for track_type, model, priority in ((forecast, rapid, high), (best, chain[0], medium)):
            ModelTrigger.objects.update_or_create(
                cyclone_track_type=track_type,
                model=model,
                country=None,
                defaults={"model_domain": domain, "priority": priority, "enabled": True},
            )

        self.stdout.write(self.style.SUCCESS("Configured:"))
        self.stdout.write(
            f"  rapid: '{forecast.name}' tracks -> {rapid.name} on riskscape.nz "
            f"({rapid.location}, {options['rapid_param']} = track URL)"
        )
        self.stdout.write(
            f"  pdna:  '{best.name}' tracks -> {' -> '.join(m.name for m in chain)} "
            f"on {options['onprem_server']}"
        )
