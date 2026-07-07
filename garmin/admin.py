from django.contrib import admin

from .models import GarminConnection


@admin.register(GarminConnection)
class GarminConnectionAdmin(admin.ModelAdmin):
    list_display = (
        "customer",
        "garmin_user_id",
        "status",
        "connected_at",
        "last_sync_at",
    )
    list_filter = ("status",)
    search_fields = ("customer__username", "garmin_user_id")
    readonly_fields = ("connected_at",)
