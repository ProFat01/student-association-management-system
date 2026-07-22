from django.contrib import admin
from django.urls import reverse
from django.utils import timezone
from django.utils.html import format_html

from .models import AlumniRecord, Member, MembershipCard, RegistrationApplication


class RegistrationApplicationInline(admin.TabularInline):
    """Read-mostly history of every application this member has ever filed."""

    model = RegistrationApplication
    extra = 0
    fields = ("application_number", "status", "submitted_at", "reviewed_at", "reviewed_by")
    readonly_fields = fields
    can_delete = False
    show_change_link = True


class AlumniRecordInline(admin.StackedInline):
    model = AlumniRecord
    extra = 0
    max_num = 1
    readonly_fields = ("converted_at",)
    autocomplete_fields = ("converted_by",)


@admin.register(Member)
class MemberAdmin(admin.ModelAdmin):
    list_display = (
        "full_name",
        "membership_id",
        "association",
        "category",
        "approval_status",
        "alumni_status",
        "voting_status",
        "registration_date",
    )
    list_filter = ("association", "approval_status", "category", "alumni_status", "voting_status")
    search_fields = ("full_name", "phone_number", "nin_number", "membership_id")
    readonly_fields = ("membership_id", "registration_date", "voting_status", "card_link")
    autocomplete_fields = ("association", "user")
    date_hierarchy = "registration_date"
    inlines = [RegistrationApplicationInline, AlumniRecordInline]
    actions = ["convert_selected_to_alumni"]

    # Member is read frequently with its association joined (list_display,
    # filters) — select_related avoids one extra query per row, the same
    # ORM-optimisation discipline used throughout the project.
    list_select_related = ("association",)

    fieldsets = (
        ("Identity", {"fields": ("association", "user", "full_name", "date_of_birth", "gender", "passport_photo")}),
        ("Contact", {"fields": ("phone_number", "nin_number")}),
        ("Academic", {"fields": ("institution", "course", "faculty", "department", "level", "category")}),
        (
            "Membership status",
            {"fields": ("approval_status", "membership_id", "voting_status", "alumni_status", "registration_date")},
        ),
        ("Membership Card", {"fields": ("card_link",)}),
    )

    @admin.display(description="Membership card")
    def card_link(self, obj):
        if not obj.pk:
            return "Save the member first."
        url = reverse("members:staff_card", args=[obj.pk])
        return format_html('<a class="button" href="{}" target="_blank">View / Print Card</a>', url)

    @admin.action(description="Convert selected members to alumni")
    def convert_selected_to_alumni(self, request, queryset):
        updated = 0
        for member in queryset:
            member.convert_to_alumni(converted_by=request.user)
            updated += 1
        self.message_user(request, f"{updated} member(s) converted to alumni.")


@admin.register(RegistrationApplication)
class RegistrationApplicationAdmin(admin.ModelAdmin):
    list_display = ("application_number", "member", "status", "submitted_at", "reviewed_at", "reviewed_by")
    list_filter = ("status", "member__association")
    search_fields = ("application_number", "member__full_name", "member__phone_number")
    autocomplete_fields = ("member",)
    readonly_fields = ("application_number", "submitted_at", "reviewed_at", "reviewed_by", "receipt_preview")
    list_select_related = ("member", "member__association", "reviewed_by")
    actions = ["approve_applications", "clear_receipt_images"]

    fieldsets = (
        ("Application", {"fields": ("application_number", "member", "submitted_at")}),
        # PART 5: receipt is shown here for review. PART 6: it's only
        # visible while a decision is still Pending — the receipt file is
        # deleted automatically the moment status leaves Pending (see
        # members/signals.py), so this preview naturally disappears once
        # a decision has been made.
        ("Payment receipt", {"fields": ("receipt_image", "receipt_preview")}),
        ("Review decision", {"fields": ("status", "rejection_reason", "reviewed_at", "reviewed_by")}),
    )

    @admin.display(description="Receipt preview")
    def receipt_preview(self, obj):
        if not obj.receipt_image:
            return "— (no receipt on file; already cleared if this application has been reviewed)"
        return format_html(
            '<a href="{0}" target="_blank" rel="noopener">'
            '<img src="{0}" style="max-height: 220px; max-width: 100%; border: 1px solid #ddd; border-radius: 4px;">'
            "</a>",
            obj.receipt_image.url,
        )

    def save_model(self, request, obj, form, change):
        # reviewed_by is set here (server-side, from the logged-in admin)
        # rather than exposed as an editable field, so it can't be
        # mis-attributed to the wrong reviewer through the form.
        if change and "status" in form.changed_data and obj.status != RegistrationApplication.Status.PENDING:
            obj.reviewed_by = request.user
        super().save_model(request, obj, form, change)

    @admin.action(description="Approve selected applications")
    def approve_applications(self, request, queryset):
        # Bulk action intentionally covers approval only: rejection
        # requires a rejection_reason (enforced in Model.clean()), which
        # only makes sense to capture per-application through the form.
        count = 0
        for application in queryset.filter(status=RegistrationApplication.Status.PENDING):
            application.status = RegistrationApplication.Status.APPROVED
            application.reviewed_by = request.user
            application.save()
            count += 1
        self.message_user(request, f"{count} application(s) approved.")

    @admin.action(description="Delete receipt images (manual fallback)")
    def clear_receipt_images(self, request, queryset):
        """
        Receipt deletion now happens automatically the instant a review
        decision is recorded (members/signals.py::_sync_member_on_review,
        PART 6 of the registration module spec) — this action exists only
        as a manual fallback (e.g. a receipt that somehow survived an
        out-of-band status change) and will typically find nothing left
        to clear.
        """
        count = 0
        for application in queryset.exclude(status=RegistrationApplication.Status.PENDING):
            if application.receipt_image:
                application.clear_receipt()
                count += 1
        self.message_user(request, f"Receipt image cleared for {count} application(s).")


@admin.register(AlumniRecord)
class AlumniRecordAdmin(admin.ModelAdmin):
    list_display = ("member", "graduation_year", "current_employer", "converted_at")
    list_filter = ("graduation_year",)
    search_fields = ("member__full_name", "current_employer")
    autocomplete_fields = ("member", "converted_by")
    list_select_related = ("member",)
