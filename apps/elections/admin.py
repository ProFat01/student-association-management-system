from django.contrib import admin

from .models import Candidate, Election, Position, Vote


class CandidateInline(admin.TabularInline):
    model = Candidate
    extra = 1
    autocomplete_fields = ("position",)
    fields = ("name", "position", "photo", "manifesto")

    def get_formset(self, request, obj=None, **kwargs):
        """
        Narrows the position dropdown to only the positions already added
        to this election's `positions` M2M — so staff can't accidentally
        nominate a candidate for an office that isn't actually on this
        election's ballot (Candidate.clean() enforces the same rule at
        the model level; this just keeps the form from offering an
        invalid choice in the first place). Only takes effect once the
        Election itself has been saved at least once (`obj` is None on
        the initial add form) — add the election's positions and save,
        then come back to add candidates.
        """
        formset = super().get_formset(request, obj, **kwargs)
        if obj is not None:
            formset.form.base_fields["position"].queryset = obj.positions.all()
        return formset


@admin.register(Position)
class PositionAdmin(admin.ModelAdmin):
    list_display = ("title", "association", "display_order")
    list_filter = ("association",)
    search_fields = ("title",)
    autocomplete_fields = ("association",)


@admin.register(Election)
class ElectionAdmin(admin.ModelAdmin):
    list_display = (
        "name", "association", "start_datetime", "end_datetime",
        "status_display", "is_enabled", "contested_positions",
    )
    list_filter = ("association", "is_enabled")
    search_fields = ("name",)
    date_hierarchy = "start_datetime"
    autocomplete_fields = ("association", "created_by")
    readonly_fields = ("created_at",)
    filter_horizontal = ("positions",)
    inlines = [CandidateInline]
    list_select_related = ("association",)

    @admin.display(description="Status")
    def status_display(self, obj):
        return obj.status.capitalize()

    @admin.display(description="Positions")
    def contested_positions(self, obj):
        return ", ".join(obj.positions.values_list("title", flat=True)) or "—"

    def get_queryset(self, request):
        return super().get_queryset(request).prefetch_related("positions")

    def save_model(self, request, obj, form, change):
        if not change and not obj.created_by:
            obj.created_by = request.user
        super().save_model(request, obj, form, change)


@admin.register(Candidate)
class CandidateAdmin(admin.ModelAdmin):
    list_display = ("name", "position", "election", "election_association")
    list_filter = ("election__association", "election", "position")
    search_fields = ("name",)
    autocomplete_fields = ("election", "position")
    list_select_related = ("election", "position", "election__association")

    @admin.display(description="Association")
    def election_association(self, obj):
        return obj.election.association


@admin.register(Vote)
class VoteAdmin(admin.ModelAdmin):
    """
    Deliberately near-immutable through the admin: votes are only ever
    meant to be created by a future member-facing voting view (with its
    own eligibility + one-vote checks), never typed in by staff. Even
    Election Admins get view-only access here for audit purposes; only a
    Django superuser can delete a vote, and that's left in purely as a
    documented emergency escape hatch (e.g. a proven data-entry/voting
    bug), not a normal workflow.
    """

    list_display = ("election", "member", "position", "candidate", "timestamp")
    list_filter = ("election__association", "election", "position")
    search_fields = ("member__full_name", "member__membership_id")
    list_select_related = ("election", "member", "candidate", "position")
    date_hierarchy = "timestamp"

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return request.user.is_superuser
