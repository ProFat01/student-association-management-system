"""
Public-facing views for the member registration module. No login is
required for any of these — registering and checking status both happen
before someone has any kind of account, by design.
"""
from django.conf import settings
from django.shortcuts import get_object_or_404, redirect, render

from apps.core.models import Association

from .forms import MemberRegistrationForm, StatusCheckForm
from .models import RegistrationApplication


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
