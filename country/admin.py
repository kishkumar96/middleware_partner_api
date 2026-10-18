from django.contrib import admin

from island.models import Island
from .models import Country


class IslandInline(admin.TabularInline):
    model = Island
    extra = 1
    min_num = 0
    validate_min = False

@admin.register(Country)
class CountryAdmin(admin.ModelAdmin):
    list_display = ('code', 'short_name', 'long_name')
    search_fields = ("code", "short_name", "long_name")
    inlines = (IslandInline,)
