"""
A single dashboard hub at /dashboard/, rather than four separate
ad-hoc pages. Each section below is gated by a permission that already
exists (declared in apps/accounts/permissions.py, attached to groups by
setup_roles) — nothing new was added to the permission architecture for
this. Sections render purely based on `request.user.has_perm(...)`, so:

  - Registration Admin sees only the Registration section
  - Election Admin sees only the Elections section
  - Analytics Admin sees only the Analytics section
  - Super Admin (and any Django superuser, who implicitly passes every
    has_perm check) sees all four sections

No data is recomputed here that already exists elsewhere — this hub
calls into apps.analytics.services (the same functions the analytics
dashboards themselves use) and links out to the existing admin
changelists / per-election dashboard rather than re-implementing any of
them.
"""
from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.shortcuts import render

from apps.analytics import services as analytics_services
from apps.core.models import Association, ContactMessage
from apps.members.models import RegistrationApplication


def _default_association():
    return Association.objects.filter(slug=settings.DEFAULT_ASSOCIATION_SLUG).first()


@login_required
def dashboard_view(request):
    user = request.user
    association = _default_association()

    sections = {
        # Gated on review_application (not the broader view_member) so
        # this card maps to "can actually process applications", which is
        # what Registration Admin is *for* — not just read access.
        "registration": user.has_perm("members.review_application"),
        # Gated on manage_election (not view_election/view_vote, which
        # Analytics Admin also holds) so this card is specifically
        # Election Admin's management view, not duplicated into
        # Analytics Admin's screen where richer election analytics
        # already exist.
        "elections": user.has_perm("elections.manage_election"),
        "analytics": user.has_perm("analytics.view_analytics_dashboard"),
        "contact": user.has_perm("core.view_contactmessage"),
    }

    context = {"sections": sections, "association": association}

    if association is not None:
        if sections["registration"]:
            context["membership"] = analytics_services.membership_overview(association)
            context["pending_applications"] = (
                RegistrationApplication.objects.filter(status=RegistrationApplication.Status.PENDING)
                .select_related("member")
                .order_by("-submitted_at")[:5]
            )

        if sections["elections"]:
            overviews = analytics_services.all_elections_overview(association)
            context["election_overviews"] = overviews
            context["active_election_overviews"] = [o for o in overviews if o["election"].status == "active"]

        if sections["analytics"]:
            context["analytics_summary"] = analytics_services.membership_overview(association)

        if sections["contact"]:
            context["unread_contact_count"] = ContactMessage.objects.filter(
                association=association, is_read=False
            ).count()
            context["recent_contact_messages"] = ContactMessage.objects.filter(association=association)[:5]

    return render(request, "accounts/dashboard.html", context)
