from django.contrib import admin
from .models import User


@admin.register(User)
class UserAdmin(admin.ModelAdmin):
    # Use ModelAdmin directly — not BaseUserAdmin, which expects
    # groups and user_permissions fields we don't have.

    list_display = ["email", "name", "role", "status", "library_id", "created_at"]
    list_filter = ["role", "status"]
    search_fields = ["email", "name"]
    ordering = ["name"]
    readonly_fields = ["user_id", "created_at"]

    fieldsets = (
        (None, {"fields": ("user_id", "email", "password")}),
        ("Personal info", {"fields": ("name", "library_id")}),
        ("Permissions", {"fields": ("role", "status")}),
        ("Dates", {"fields": ("created_at", "last_login")}),
    )
