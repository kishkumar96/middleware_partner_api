"""Imports the Cook Islands dimensional risk analysis (SLR x return-period/
event, mirroring national-impact.csv / national-average-loss.csv) into
RiskInformation (one parent row per hazard product) + RiskScenario (one
child row per SLR x return-period/event combination).

Source layout (see data/ck_risk_analysis/README.txt), one folder per
product, matching hazard_tiles.views.PRODUCTS exactly:
  HistTC          -> historical_tcs  (event-based)
  SynTC           -> synthetic_tcs   (event-based)
  Swell           -> swell_driven    (event-based)
  Prob_TC_AAL     -> tc              (probabilistic: return-period + AAL)
  Prob_swell_AAL  -> swell           (probabilistic: return-period + AAL)
  Prob_combo_AAL  -> combined        (probabilistic: return-period + AAL)

Each folder's national-impact.csv holds one row per (SLR, Event) or (SLR,
Return_Period). Probabilistic folders additionally have
national-average-loss.csv, one row per SLR (the annual average loss,
no return-period/event dimension).

Safe to re-run: RiskScenario rows are matched by their own natural key
(risk_information, slr_scenario_m, return_period_years, event_label) and
left untouched; the RiskInformation parent per product is matched by
(country, title).
"""

import csv
import os

from django.conf import settings
from django.core.files.base import ContentFile
from django.core.management.base import BaseCommand

from country.models import Country
from risk_information.models import RiskInformation, RiskScenario

DATA_DIR = os.path.join(settings.BASE_DIR, "data", "ck_risk_analysis")

# folder -> (RiskInformation title, is event-based)
FOLDER_TO_PRODUCT = {
    "HistTC": ("CK historical_tcs national impact", True),
    "SynTC": ("CK synthetic_tcs national impact", True),
    "Swell": ("CK swell_driven national impact", True),
    "Prob_TC_AAL": ("CK tc national impact + AAL", False),
    "Prob_swell_AAL": ("CK swell national impact + AAL", False),
    "Prob_combo_AAL": ("CK combined national impact + AAL", False),
}

IMPACT_FIELD_MAP = {
    "Exposed.Building_Value": "exposed_building_value",
    "Exposed.Road_Value": "exposed_road_value",
    "Exposed.Infrastructure_Value": "exposed_infrastructure_value",
    "Exposed.Crop_Value": "exposed_crop_value",
    "Exposed.Population": "exposed_population",
    "Exposed.Total_Value": "exposed_total_value",
    "Exposed.Road_km": "exposed_road_km",
    "Loss.Total": "total_loss",
    "Loss.Building_Loss": "building_loss",
    "Loss.Road_Loss": "road_loss",
    "Loss.Infrastructure_Loss": "infrastructure_loss",
    "Loss.Crop_Loss": "crop_loss",
}

AAL_FIELD_MAP = {
    "AAL_Exposed_Building_Value": "exposed_building_value",
    "AAL_Exposed_Road_Value": "exposed_road_value",
    "AAL_Exposed_Infrastructure_Value": "exposed_infrastructure_value",
    "AAL_Exposed_Crop_Value": "exposed_crop_value",
    "AAL_Exposed_Population": "exposed_population",
    "AAL_Exposed_Total_Value": "exposed_total_value",
    "AAL_Exposed_Road_km": "exposed_road_km",
    "AAL_Total": "total_loss",
    "AAL_Buildings": "building_loss",
    "AAL_Roads": "road_loss",
    "AAL_Infrastructure": "infrastructure_loss",
    "AAL_Crops": "crop_loss",
}


def to_float(value):
    if value is None or value == "":
        return None
    try:
        return float(value)
    except ValueError:
        return None


class Command(BaseCommand):
    help = "Import Cook Islands dimensional risk analysis (national-impact/national-average-loss CSVs) into RiskInformation/RiskScenario."

    def read_csv_rows(self, path):
        with open(path, newline="", encoding="utf-8-sig") as f:
            return list(csv.DictReader(f))

    def get_or_create_parent(self, country, title):
        obj, created = RiskInformation.objects.get_or_create(
            country=country,
            title=title,
            defaults={},
        )
        if created:
            placeholder = f"Aggregated from {title} (see RiskScenario rows).".encode("utf-8")
            obj.upload.save(f"{title.replace(' ', '_')}.txt", ContentFile(placeholder), save=True)
        return obj

    def handle(self, *args, **options):
        country = Country.objects.filter(short_name="CK").first()
        if country is None:
            self.stderr.write("Cook Islands country row not found (expected short_name='CK').")
            return

        created, skipped, failed = 0, 0, 0

        for folder, (title, is_event_based) in FOLDER_TO_PRODUCT.items():
            folder_path = os.path.join(DATA_DIR, folder)
            impact_csv = os.path.join(folder_path, "national-impact.csv")
            if not os.path.exists(impact_csv):
                self.stderr.write(f"Missing {impact_csv}, skipping {folder}.")
                failed += 1
                continue

            risk_info = self.get_or_create_parent(country, title)

            for row in self.read_csv_rows(impact_csv):
                slr = to_float(row.get("SLR"))
                if slr is None:
                    failed += 1
                    continue

                event_label = row.get("Event") if is_event_based else None
                return_period = None if is_event_based else int(float(row["Return_Period"]))

                _, was_created = RiskScenario.objects.get_or_create(
                    risk_information=risk_info,
                    slr_scenario_m=slr,
                    return_period_years=return_period,
                    event_label=event_label,
                    defaults={
                        "is_annual_average_loss": False,
                        **{
                            model_field: to_float(row.get(csv_col))
                            for csv_col, model_field in IMPACT_FIELD_MAP.items()
                        },
                    },
                )
                created += was_created
                skipped += not was_created

            if is_event_based:
                continue

            aal_csv = os.path.join(folder_path, "national-average-loss.csv")
            if not os.path.exists(aal_csv):
                self.stderr.write(f"Missing {aal_csv}, skipping AAL rows for {folder}.")
                failed += 1
                continue

            for row in self.read_csv_rows(aal_csv):
                slr = to_float(row.get("SLR"))
                if slr is None:
                    failed += 1
                    continue

                _, was_created = RiskScenario.objects.get_or_create(
                    risk_information=risk_info,
                    slr_scenario_m=slr,
                    return_period_years=None,
                    event_label=None,
                    defaults={
                        "is_annual_average_loss": True,
                        **{
                            model_field: to_float(row.get(csv_col))
                            for csv_col, model_field in AAL_FIELD_MAP.items()
                        },
                    },
                )
                created += was_created
                skipped += not was_created

        self.stdout.write(
            self.style.SUCCESS(
                f"Done. Created {created} RiskScenario rows, already existed {skipped}, failed {failed}."
            )
        )
