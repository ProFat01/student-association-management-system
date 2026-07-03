"""
Views for the analytics module: PART 10's staff dashboards and PART 11's
JSON API endpoints. Every view in this file is gated by
`analytics_staff_required` (PART 12) — there is no public view here,
unlike the members/elections modules.

All computation lives in services.py; these views only resolve the
Association/Election from the URL, call a service function, and either
render a template or return JsonResponse. Kept deliberately thin so the
JSON endpoints and the HTML dashboards that show the same numbers can
never drift apart — both call the exact same service function.
"""
from functools import wraps

from django.conf import settings
from django.contrib.auth.decorators import login_required, permission_required
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, render

from apps.core.models import Association
from apps.elections.models import Election

from . import services


def analytics_staff_required(view_func):
    """
    PART 12: only Analytics Admin / Super Admin may access analytics
    views. Stacks login_required (anonymous -> redirect to admin login)
    with permission_required(..., raise_exception=True) (authenticated
    but lacking the permission -> 403, not an endless login redirect) —
    the same combination the election module's admin dashboard already
    uses, applied here to *every* view in this file via one decorator
    instead of repeating both on each view.
    """

    @wraps(view_func)
    @login_required
    @permission_required("analytics.view_analytics_dashboard", raise_exception=True)
    def wrapped(request, *args, **kwargs):
        return view_func(request, *args, **kwargs)

    return wrapped


def _default_association():
    return Association.objects.filter(slug=settings.DEFAULT_ASSOCIATION_SLUG).first()


# ---------------------------------------------------------------------------
# PART 10: Dashboard pages
# ---------------------------------------------------------------------------
@analytics_staff_required
def overview_dashboard_view(request):
    association = _default_association()
    context = {
        "association": association,
        "membership": services.membership_overview(association) if association else None,
        "elections": services.all_elections_overview(association) if association else [],
    }
    return render(request, "analytics/overview_dashboard.html", context)


@analytics_staff_required
def membership_dashboard_view(request):
    association = _default_association()
    granularity = request.GET.get("granularity", "month")
    context = {
        "association": association,
        "membership": services.membership_overview(association) if association else None,
        "growth": services.registration_growth(association, granularity) if association else [],
        "granularity": granularity,
    }
    return render(request, "analytics/membership_dashboard.html", context)


@analytics_staff_required
def course_dashboard_view(request):
    association = _default_association()
    order = request.GET.get("order", "desc")
    context = {
        "association": association,
        "rows": services.course_distribution(association, order) if association else [],
        "order": order,
    }
    return render(request, "analytics/course_dashboard.html", context)


@analytics_staff_required
def institution_dashboard_view(request):
    association = _default_association()
    order = request.GET.get("order", "desc")
    context = {
        "association": association,
        "rows": services.institution_distribution(association, order) if association else [],
        "order": order,
    }
    return render(request, "analytics/institution_dashboard.html", context)


@analytics_staff_required
def age_dashboard_view(request):
    association = _default_association()
    context = {
        "association": association,
        "rows": services.age_distribution(association) if association else [],
    }
    return render(request, "analytics/age_dashboard.html", context)


@analytics_staff_required
def election_dashboard_list_view(request):
    association = _default_association()
    context = {
        "association": association,
        "elections": services.all_elections_overview(association) if association else [],
    }
    return render(request, "analytics/election_dashboard_list.html", context)


@analytics_staff_required
def election_dashboard_detail_view(request, pk):
    election = get_object_or_404(Election.objects.select_related("association"), pk=pk)
    context = {
        "election": election,
        "overview": services.election_overview(election),
        "results": services.position_results_with_winner(election),
        "age_participation": services.age_participation(election),
    }
    return render(request, "analytics/election_dashboard_detail.html", context)


# ---------------------------------------------------------------------------
# PART 11: JSON API endpoints — clean, chart-library-ready data only.
# No JS charting is wired up here on purpose; these just return JSON.
# ---------------------------------------------------------------------------
@analytics_staff_required
def api_membership_statistics(request):
    association = _default_association()
    return JsonResponse(services.membership_overview(association) if association else {})


@analytics_staff_required
def api_course_statistics(request):
    association = _default_association()
    order = request.GET.get("order", "desc")
    rows = services.course_distribution(association, order) if association else []
    return JsonResponse({"order": order, "results": rows})


@analytics_staff_required
def api_institution_statistics(request):
    association = _default_association()
    order = request.GET.get("order", "desc")
    rows = services.institution_distribution(association, order) if association else []
    return JsonResponse({"order": order, "results": rows})


@analytics_staff_required
def api_age_distribution(request):
    association = _default_association()
    rows = services.age_distribution(association) if association else []
    return JsonResponse({"results": rows})


@analytics_staff_required
def api_registration_growth(request):
    """Bonus endpoint (not in PART 11's explicit list, but directly fulfils PART 5's 'helper methods for future chart integration')."""
    association = _default_association()
    granularity = request.GET.get("granularity", "month")
    rows = services.registration_growth(association, granularity) if association else []
    return JsonResponse({"granularity": granularity, "results": rows})


@analytics_staff_required
def api_election_results(request, pk):
    election = get_object_or_404(Election, pk=pk)
    results = services.position_results_with_winner(election)
    payload = [
        {
            "position": item["position"].title,
            "total_votes": item["total_votes"],
            "is_tie": item["is_tie"],
            "winner": item["winner"].name if item["winner"] else None,
            "candidates": [
                {"name": row["candidate"].name, "vote_count": row["vote_count"], "percentage": row["percentage"]}
                for row in item["candidates"]
            ],
        }
        for item in results
    ]
    return JsonResponse({"election": election.name, "results": payload})


@analytics_staff_required
def api_election_turnout(request, pk):
    election = get_object_or_404(Election, pk=pk)
    overview = services.election_overview(election)
    return JsonResponse(
        {
            "election": election.name,
            "eligible_voters": overview["eligible_voters"],
            "votes_cast": overview["votes_cast"],
            "turnout_percentage": overview["turnout_percentage"],
            "total_positions": overview["total_positions"],
            "total_candidates": overview["total_candidates"],
            "age_participation": services.age_participation(election),
        }
    )
