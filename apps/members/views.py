"""
Public-facing views for the member registration module. No login is
required for any of these — registering and checking status both happen
before someone has any kind of account, by design.

The portal_* views below (Stage 8: Member Self-Service Portal) are the
one exception, but deliberately don't introduce real Django auth for
Member — same reasoning apps.elections.views gives for voting login:
Members don't have User accounts, so "logging in" is a lightweight
session marker set after PortalLoginForm.authenticate() succeeds, not
request.login(). See _get_portal_member below.
"""
from django.conf import settings
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from apps.core.models import Association

from .forms import MemberRegistrationForm, PortalLoginForm, StatusCheckForm
from .models import Member, RegistrationApplication

PORTAL_SESSION_KEY = "portal_member_id"


def _get_default_association():
    """
    Single-tenant lookup for now: resolves the one Association this
    deployment registers members against, via
    settings.DEFAULT_ASSOCIATION_SLUG (already part of the project's
    multi-tenancy scaffolding — see core/models.py). Returns None if it
    hasn't been created yet (e.g. a fresh install before anyone has used
    the admin to set up the Association row), so callers can fail
    gracefully instead of raising.
    """
    return Association.objects.filter(slug=settings.DEFAULT_ASSOCIATION_SLUG).first()


def register_view(request):
    association = _get_default_association()
    if association is None:
        return render(request, "members/register.html", {"association_missing": True})

    if request.method == "POST":
        form = MemberRegistrationForm(request.POST, request.FILES)
        if form.is_valid():
            application = form.save(association=association)
            return redirect("members:registration_success", application_number=application.application_number)
    else:
        form = MemberRegistrationForm()

    return render(
        request,
        "members/register.html",
        {
            "form": form,
            "association_missing": False,
            "show_recovery_cta": getattr(form, "duplicate_detected", False),
        },
    )


def registration_success_view(request, application_number):
    application = get_object_or_404(
        RegistrationApplication.objects.select_related("member"),
        application_number=application_number,
    )
    return render(request, "members/registration_success.html", {"application": application})


def status_check_view(request):
    """
    One view, one URL, both GET (empty form) and POST (run the search) —
    keeps a bookmarkable/shareable form page while still rendering the
    PART 9-required separate result template as an included partial once
    a search has actually been performed.
    """
    searched = False
    result = None

    if request.method == "POST":
        form = StatusCheckForm(request.POST)
        if form.is_valid():
            searched = True
            result = form.lookup()
    else:
        form = StatusCheckForm()

    return render(
        request,
        "members/status_check.html",
        {"form": form, "searched": searched, "result": result},
    )


# ---------------------------------------------------------------------------
# Stage 8: Member Self-Service Portal
# ---------------------------------------------------------------------------
def _get_portal_member(request):
    """
    Returns the Member logged into the self-service portal this session,
    or None. Re-checks approval_status on every call (not just at login)
    so a member who is later rejected/suspended loses portal access on
    their very next request, the same "recheck live, don't trust what
    was true at login" pattern elections._get_voting_member follows for
    the election window.
    """
    member_id = request.session.get(PORTAL_SESSION_KEY)
    if not member_id:
        return None
    return (
        Member.objects.filter(pk=member_id, approval_status=Member.ApprovalStatus.APPROVED)
        .select_related("association")
        .first()
    )


def portal_login_view(request):
    error_message = None

    if request.method == "POST":
        form = PortalLoginForm(request.POST)
        if form.is_valid():
            member, error_code = form.authenticate()
            if member is not None:
                request.session.cycle_key()  # mitigate session fixation on this privilege boundary
                request.session[PORTAL_SESSION_KEY] = member.pk
                return redirect("members:portal_dashboard")
            error_message = (
                "Membership not found."
                if error_code == PortalLoginForm.NOT_FOUND
                else "Your membership has not yet been approved."
            )
    else:
        form = PortalLoginForm()

    return render(request, "members/portal_login.html", {"form": form, "error_message": error_message})


def _portal_election_context(member):
    """
    Finds the one election most relevant to show on the portal dashboard
    right now (open for voting, else soonest upcoming, else most recently
    closed) and derives the Vote Now eligibility purely from methods
    Election/Member already expose (is_voting_open is the model's own
    documented single source of truth for "can a member vote right now"
    — see its docstring in elections/models.py) — no new elections-app
    code needed for this. Imported locally, matching the existing
    cross-app-import convention apps.elections.views._get_voting_member
    already uses the other way around (importing apps.members.models
    from within apps.elections).
    """
    from apps.elections.models import Election

    elections = list(Election.objects.filter(association=member.association, is_enabled=True))
    open_now = [election for election in elections if election.is_voting_open]
    upcoming = sorted((e for e in elections if e.is_upcoming()), key=lambda e: e.start_datetime)
    closed = sorted((e for e in elections if e.is_closed()), key=lambda e: e.end_datetime, reverse=True)

    current_election = None
    election_state = "none"
    can_vote = False

    if open_now:
        current_election = open_now[0]
        already_voted = current_election.has_member_voted(member)
        election_state = "voted" if already_voted else "open"
        can_vote = member.voting_status and not already_voted
    elif upcoming:
        current_election = upcoming[0]
        election_state = "upcoming"
    elif closed:
        current_election = closed[0]
        election_state = "closed"

    return {"current_election": current_election, "election_state": election_state, "can_vote": can_vote}


def portal_dashboard_view(request):
    member = _get_portal_member(request)
    if member is None:
        return redirect("members:portal_login")

    context = {"member": member}
    context.update(_portal_election_context(member))
    return render(request, "members/portal_dashboard.html", context)


def portal_profile_view(request):
    """View-only detail page — no form, so there's nothing here that could edit a field."""
    member = _get_portal_member(request)
    if member is None:
        return redirect("members:portal_login")
    return render(request, "members/portal_profile.html", {"member": member})


@require_POST
def portal_logout_view(request):
    # Full session flush (not just popping PORTAL_SESSION_KEY) per the
    # brief's "Destroy session after logout" — a clean privilege
    # boundary rather than a partial clear.
    request.session.flush()
    return redirect("members:portal_login")
