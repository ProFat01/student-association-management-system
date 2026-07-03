from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin

from .models import User


@admin.register(User)
class UserAdmin(DjangoUserAdmin):
    """
    Extends Django's battle-tested UserAdmin (password hashing widget,
    permission/group management UI, etc.) rather than rebuilding it, and
    only adds what's actually new on this model: `association` (tenant
    scoping) and `phone_number`.
    """

    fieldsets = DjangoUserAdmin.fieldsets + (
        ("SAMS", {"fields": ("association", "phone_number")}),
    )
    add_fieldsets = DjangoUserAdmin.add_fieldsets + (
        ("SAMS", {"fields": ("association", "phone_number")}),
    )
    list_display = DjangoUserAdmin.list_display + ("association", "role_list")
    list_filter = DjangoUserAdmin.list_filter + ("association", "groups")
    autocomplete_fields = ("association",)

    @admin.display(description="Roles")
    def role_list(self, obj):
        return ", ".join(obj.role_names) or "—"
