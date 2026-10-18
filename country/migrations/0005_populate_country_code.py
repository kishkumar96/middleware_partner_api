from django.db import migrations

from country.codes import code_for


def populate_codes(apps, schema_editor):
    """Give each existing country the code its names imply; leave unrecognised ones for an operator."""
    Country = apps.get_model("country", "Country")
    taken = set(Country.objects.exclude(code__isnull=True).values_list("code", flat=True))
    for country in Country.objects.filter(code__isnull=True).order_by("id"):
        code = code_for(country.short_name, country.long_name)
        # Two rows that mean the same country: the first keeps the code, the other is left blank
        if code and code not in taken:
            country.code = code
            country.save(update_fields=["code"])
            taken.add(code)


class Migration(migrations.Migration):
    dependencies = [
        ("country", "0004_country_code"),
    ]

    operations = [
        migrations.RunPython(populate_codes, migrations.RunPython.noop),
    ]
