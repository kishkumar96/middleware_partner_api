"""Imports the Cook Islands dimensional risk analysis (SLR x return-period/
event, mirroring national-impact.csv / national-average-loss.csv) into
RiskInformation (one parent row per hazard product) + RiskScenario (one
child row per SLR x return-period/event combination) + RiskScenarioBreakdown
(that scenario's loss by sector and by asset type).

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
no return-period/event dimension), and aal_by_sector.csv.
impact-asset-sector.csv and impact-asset-type.csv break each national row
down by sector and by asset type.

Reconciliation: a scenario's asset-type losses must sum to its national
loss. Prob_combo_AAL's national-impact.csv as delivered has 22 of its 24
rows under the wrong (SLR, Return_Period) label: every row matches exactly
one scenario's breakdown on all four loss components, and only relabelled
do losses rise with return period and SLR. When national rows don't
reconcile but each matches exactly one breakdown scenario on every loss
component, they're relabelled to that scenario (and reported); otherwise
they're imported as-is with a warning.

Safe to re-run: RiskScenario rows are matched by their natural key
(risk_information, slr_scenario_m, return_period_years, event_label) and
their values updated; a product's breakdown rows are replaced; the
RiskInformation parent per product is matched by (country, title).
"""

import csv
import os
from collections import defaultdict

from django.conf import settings
from django.core.files.base import ContentFile
from django.core.management.base import BaseCommand
from django.db import transaction

from country.models import Country
from risk_information.models import RiskInformation, RiskScenario, RiskScenarioBreakdown

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

ASSET_TYPE_FIELD_MAP = {
    "Exposed_Count": "exposed_count",
    "Total_Count": "total_count",
    "Total_Exposed_Value": "exposed_value",
    "Total_Value": "total_value",
    "Total_Exposed_Area_Or_Length": "exposed_area_or_length",
    "Total_Area_Or_Length": "total_area_or_length",
    "Average_Inundation_Depth": "average_inundation_depth",
    "Max_Inundation_Depth": "max_inundation_depth",
    "Total_Loss.Total": "total_loss",
    "Total_Loss.Building_Loss": "building_loss",
    "Total_Loss.Road_Loss": "road_loss",
    "Total_Loss.Infrastructure_Loss": "infrastructure_loss",
    "Total_Loss.Crop_Loss": "crop_loss",
}

# National column -> asset-type column, compared when reconciling
LOSS_COMPONENTS = {
    "Loss.Total": "Total_Loss.Total",
    "Loss.Building_Loss": "Total_Loss.Building_Loss",
    "Loss.Road_Loss": "Total_Loss.Road_Loss",
    "Loss.Infrastructure_Loss": "Total_Loss.Infrastructure_Loss",
    "Loss.Crop_Loss": "Total_Loss.Crop_Loss",
}


def to_float(value):
    if value is None or value == "":
        return None
    try:
        return float(value)
    except ValueError:
        return None


def close(a, b):
    # Source values are rounded to whole dollars per row
    return abs(a - b) <= max(2.0, 1e-4 * max(abs(a), abs(b)))


class Command(BaseCommand):
    help = (
        "Import Cook Islands dimensional risk analysis (national-impact/national-average-loss "
        "CSVs plus sector/asset-type breakdowns) into RiskInformation/RiskScenario/RiskScenarioBreakdown."
    )

    def add_arguments(self, parser):
        parser.add_argument("--data-dir", default=DATA_DIR)

    def read_csv_rows(self, path):
        if not os.path.exists(path):
            return None
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

        totals = defaultdict(int)
        for folder, (title, is_event_based) in FOLDER_TO_PRODUCT.items():
            folder_path = os.path.join(options["data_dir"], folder)
            impact_rows = self.read_csv_rows(os.path.join(folder_path, "national-impact.csv"))
            if impact_rows is None:
                self.stderr.write(f"Missing {folder}/national-impact.csv, skipping {folder}.")
                totals["failed"] += 1
                continue
            with transaction.atomic():
                self.import_product(country, folder, folder_path, title, is_event_based, impact_rows, totals)

        self.stdout.write(
            self.style.SUCCESS(
                f"Done. RiskScenario rows: created {totals['created']}, updated {totals['updated']}, "
                f"relabelled {totals['relabelled']}, failed {totals['failed']}. "
                f"Breakdown rows: {totals['breakdowns']} ({totals['unmatched']} without a matching scenario)."
            )
        )

    def import_product(self, country, folder, folder_path, title, is_event_based, impact_rows, totals):
        def key(row):
            slr = to_float(row.get("SLR"))
            if slr is None:
                return None
            if is_event_based:
                return (slr, None, row.get("Event"))
            return (slr, int(float(row["Return_Period"])), None)

        asset_rows = self.read_csv_rows(os.path.join(folder_path, "impact-asset-type.csv")) or []
        sector_rows = self.read_csv_rows(os.path.join(folder_path, "impact-asset-sector.csv")) or []

        national = {}
        for row in impact_rows:
            k = key(row)
            if k is None:
                totals["failed"] += 1
                continue
            national[k] = row
        national, relabelled = self.reconcile(folder, national, asset_rows, key)
        totals["relabelled"] += relabelled

        risk_info = self.get_or_create_parent(country, title)
        scenario_ids = {}
        for (slr, return_period, event_label), row in national.items():
            scenario, was_created = RiskScenario.objects.update_or_create(
                risk_information=risk_info,
                slr_scenario_m=slr,
                return_period_years=return_period,
                event_label=event_label,
                defaults={
                    "is_annual_average_loss": False,
                    **{field: to_float(row.get(col)) for col, field in IMPACT_FIELD_MAP.items()},
                },
            )
            scenario_ids[(slr, return_period, event_label)] = scenario.pk
            totals["created" if was_created else "updated"] += 1

        aal_ids = {}
        if not is_event_based:
            aal_rows = self.read_csv_rows(os.path.join(folder_path, "national-average-loss.csv"))
            if aal_rows is None:
                self.stderr.write(f"Missing {folder}/national-average-loss.csv, skipping AAL rows.")
                totals["failed"] += 1
            for row in aal_rows or []:
                slr = to_float(row.get("SLR"))
                if slr is None:
                    totals["failed"] += 1
                    continue
                scenario, was_created = RiskScenario.objects.update_or_create(
                    risk_information=risk_info,
                    slr_scenario_m=slr,
                    return_period_years=None,
                    event_label=None,
                    defaults={
                        "is_annual_average_loss": True,
                        **{field: to_float(row.get(col)) for col, field in AAL_FIELD_MAP.items()},
                    },
                )
                aal_ids[slr] = scenario.pk
                totals["created" if was_created else "updated"] += 1

        RiskScenarioBreakdown.objects.filter(risk_scenario__risk_information=risk_info).delete()
        breakdowns = []
        unmatched = set()

        def add(scenario_id, missing_key, **fields):
            if scenario_id is None:
                unmatched.add(missing_key)
                return
            breakdowns.append(RiskScenarioBreakdown(risk_scenario_id=scenario_id, **fields))

        for row in sector_rows:
            k = key(row)
            add(
                scenario_ids.get(k),
                k,
                dimension=RiskScenarioBreakdown.SECTOR,
                name=row["Sector"],
                total_loss=to_float(row.get("Total_Loss.Total")),
            )
        for row in asset_rows:
            k = key(row)
            add(
                scenario_ids.get(k),
                k,
                dimension=RiskScenarioBreakdown.ASSET_TYPE,
                name=row["UseType"],
                sector=row.get("Sector") or "",
                **{field: to_float(row.get(col)) for col, field in ASSET_TYPE_FIELD_MAP.items()},
            )
        if not is_event_based:
            for row in self.read_csv_rows(os.path.join(folder_path, "aal_by_sector.csv")) or []:
                slr = to_float(row.get("SLR"))
                add(
                    aal_ids.get(slr),
                    ("AAL", slr),
                    dimension=RiskScenarioBreakdown.SECTOR,
                    name=row["Sector"],
                    total_loss=to_float(row.get("AAL")),
                )

        RiskScenarioBreakdown.objects.bulk_create(breakdowns, batch_size=5000)
        totals["breakdowns"] += len(breakdowns)
        if unmatched:
            totals["unmatched"] += len(unmatched)
            self.stderr.write(
                f"{folder}: {len(unmatched)} breakdown scenario(s) have no national row, e.g. {sorted(unmatched, key=str)[:3]}"
            )

    def reconcile(self, folder, national, asset_rows, key):
        """Check national rows against their asset-type breakdown sums, and
        relabel them when they're demonstrably under the wrong scenario key.

        Returns (national rows by key, number relabelled)."""
        if not asset_rows:
            return national, 0

        sums = defaultdict(lambda: defaultdict(float))
        for row in asset_rows:
            k = key(row)
            for asset_col in LOSS_COMPONENTS.values():
                sums[k][asset_col] += to_float(row.get(asset_col)) or 0.0

        def matches(national_row, breakdown_key):
            return all(
                close(to_float(national_row.get(nat_col)) or 0.0, sums[breakdown_key][asset_col])
                for nat_col, asset_col in LOSS_COMPONENTS.items()
            )

        mismatched = [k for k, row in national.items() if k in sums and not matches(row, k)]
        if not mismatched:
            return national, 0

        # Each national row must match exactly one breakdown scenario, and no
        # two rows the same one; anything less and relabelling would be a guess.
        relabelled = {}
        for k, row in national.items():
            candidates = [bk for bk in sums if matches(row, bk)]
            if len(candidates) != 1:
                relabelled = None
                break
            relabelled[candidates[0]] = row
        if relabelled is None or len(relabelled) != len(national):
            self.stderr.write(
                self.style.WARNING(
                    f"{folder}: {len(mismatched)} national row(s) don't match their sector/asset "
                    f"breakdown, e.g. {mismatched[:3]}; importing as-is."
                )
            )
            return national, 0

        moved = sum(1 for k, row in national.items() if relabelled.get(k) is not row)
        self.stderr.write(
            self.style.WARNING(
                f"{folder}: {moved} national-impact.csv row(s) were under the wrong scenario label; "
                "relabelled to the scenario whose sector/asset breakdown they match exactly."
            )
        )
        return relabelled, moved
