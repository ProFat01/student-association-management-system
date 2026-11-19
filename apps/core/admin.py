from django.contrib import admin
from django.utils import timezone

from .admin_badges import status_badge
from .models import Association, ContactMessage, Leadership, SequenceCounter, SiteSettings

# Version 1.2 "Admin Experience" polish -- cosmetic labelling only via
# the AdminSite's own documented attributes, not a template override, so
# the stock admin/base_site.html (including its login-page colour-theme
# toggle) is left completely untouched.
admin.site.site_header = "SAMS Administration"
admin.site.site_title = "SAMS Admin"
admin.site.index_title = "Dashboard"


REGISTRATION_PERMISSION = "core.manage_registration_status"


class RegistrationControlMixin:
    """
    Makes the "registration is open" switch editable only by users holding
    core.manage_registration_status. Everyone else who can edit Site Settings
    still sees the current status, but read-only, so a posted form can never
    change it (Django drops read-only fields from the submitted form).
    """

    def get_readonly_fields(self, request, obj=None):
        readonly = list(super().get_readonly_fields(request, obj))
        if not request.user.has_perm(REGISTRATION_PERMISSION):
            readonly.append("registration_open")
        return readonly


class SiteSettingsInline(RegistrationControlMixin, admin.StackedInline):
    model = SiteSettings
    extra = 0
    max_num = 1
    can_delete = False
    fieldsets = (
        (
            "Membership registration",
            {
                "fields": ("registration_open",),
                "description": "Open or close public membership registration. Takes effect immediately.",
            },
        ),
        ("Branding", {"fields": ("motto", "welcome_message", "hero_image")}),
        ("About page content", {"fields": ("about_text", "mission", "vision", "leadership_text")}),
        ("Contact details", {"fields": ("contact_email", "contact_phone", "address")}),
        ("Donations", {"fields": ("donation_details",)}),
        ("Social links", {"fields": ("facebook_url", "x_url", "instagram_url", "whatsapp_url")}),
    )


class LeadershipInline(admin.TabularInline):
    model = Leadership
    extra = 1
    fields = ("full_name", "position", "photo", "facebook_url", "display_order", "is_active")


@admin.register(Association)
class AssociationAdmin(admin.ModelAdmin):
    list_display = ("short_name", "name", "is_active", "established_year", "created_at")
    list_filter = ("is_active",)
    search_fields = ("name", "short_name", "slug")
    prepopulated_fields = {"slug": ("short_name",)}
    readonly_fields = ("created_at",)
    inlines = [SiteSettingsInline, LeadershipInline]


@admin.register(SiteSettings)
class SiteSettingsAdmin(RegistrationControlMixin, admin.ModelAdmin):
    """
    Registered standalone too (in addition to the inline above) so a
    Registration/Election Admin with only `view` on SiteSettings can find
    it directly without needing access to the parent Association record.
    """

    list_display = ("association", "registration_status", "motto", "contact_email", "contact_phone", "updated_at")
    list_filter = ("registration_open",)
    search_fields = ("association__name", "contact_email")
    readonly_fields = ("updated_at",)
    autocomplete_fields = ("association",)
    fieldsets = SiteSettingsInline.fieldsets + (("Record", {"fields": ("association", "updated_at")}),)
    actions = ["open_registration", "close_registration"]

    @admin.display(description="Membership registration", ordering="registration_open")
    def registration_status(self, obj):
        if obj.registration_open:
            return status_badge("Open", "success")
        return status_badge("Closed", "error")

    def has_manage_registration_status_permission(self, request):
        # Used by the two actions below (Django's `permissions=` hook).
        return request.user.has_perm(REGISTRATION_PERMISSION)

    @admin.action(description="Open membership registration", permissions=["manage_registration_status"])
    def open_registration(self, request, queryset):
        updated = queryset.update(registration_open=True, updated_at=timezone.now())
        self.message_user(request, f"Membership registration is now OPEN ({updated} record(s) updated).")

    @admin.action(description="Close membership registration", permissions=["manage_registration_status"])
    def close_registration(self, request, queryset):
        updated = queryset.update(registration_open=False, updated_at=timezone.now())
        self.message_user(request, f"Membership registration is now CLOSED ({updated} record(s) updated).")


@admin.register(Leadership)
class LeadershipAdmin(admin.ModelAdmin):
    """
    Registered standalone too (in addition to the inline above), same
    reasoning as SiteSettingsAdmin: lets an admin with access scoped to
    Leadership find and manage leaders directly.
    """

    list_display = ("full_name", "position", "association", "display_order", "is_active")
    list_filter = ("association", "is_active")
    search_fields = ("full_name", "position")
    list_editable = ("display_order", "is_active")
    autocomplete_fields = ("association",)


@admin.register(ContactMessage)
class ContactMessageAdmin(admin.ModelAdmin):
    list_display = ("subject", "name", "email", "association", "submitted_at", "is_read")
    list_filter = ("is_read", "association")
    search_fields = ("name", "email", "subject", "message")
    readonly_fields = ("association", "name", "email", "subject", "message", "submitted_at")
    list_editable = ("is_read",)
    date_hierarchy = "submitted_at"
    actions = ["mark_as_read", "mark_as_unread"]

    def has_add_permission(self, request):
        return False  # these only ever come in through the public contact form

    @admin.action(description="Mark selected messages as read")
    def mark_as_read(self, request, queryset):
        updated = queryset.update(is_read=True)
        self.message_user(request, f"{updated} message(s) marked as read.")

    @admin.action(description="Mark selected messages as unread")
    def mark_as_unread(self, request, queryset):
        updated = queryset.update(is_read=False)
        self.message_user(request, f"{updated} message(s) marked as unread.")


@admin.register(SequenceCounter)
class SequenceCounterAdmin(admin.ModelAdmin):
    """
    Read-only: these rows are only ever mutated atomically by
    core.utils.get_next_sequence(). Hand-editing one risks duplicate
    membership IDs / application numbers, so admin can view but not
    add/change/delete.
    """

    list_display = ("association", "key", "last_value")
    list_filter = ("association",)
    search_fields = ("key",)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
