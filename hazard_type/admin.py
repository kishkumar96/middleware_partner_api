from django.contrib import admin

from .models import HazardSource, HazardType


@admin.register(HazardType)
class HazardTypeAdmin(admin.ModelAdmin):
    list_display = ("id", "hazard_type")
    search_fields = ("hazard_type",)


@admin.register(HazardSource)
class HazardSourceAdmin(admin.ModelAdmin):
    list_display = ("id", "name")
    search_fields = ("name",)
