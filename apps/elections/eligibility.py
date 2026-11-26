"""
Election Eligibility Engine (Version 2.0).

Single source of truth for "who is allowed to vote in this election".
Every module that needs to answer that question — the voting views,
Election.eligible_voters_count()/turnout_percentage(), the analytics
dashboards, a future Communication Center integration — must call into
this module rather than re-deriving the rules itself. There is
deliberately no eligibility logic duplicated anywhere else in the
codebase; everything else calls eligible_members()/is_member_eligible()
below, directly or via Election.eligible_members()/is_member_eligible().

Rule-driven, not hardcoded: an Election declares its eligibility as data
(Election.scope + the eligibility_* filter fields — see models.py), and
this module turns that data into a database query. Adding a brand new
kind of election (e.g. a future "Postgraduate Representative" election)
never requires touching this file — only a different combination of
filter values on the Election row.

Performance: every function here does its filtering at the database
level via QuerySet chaining (never `for member in Member.objects.all()`
in Python), and every filter added below is an indexed or simple
equality/iexact lookup — safe on SQLite / PythonAnywhere Free at the
scale this system runs at. See models.Member.Meta.indexes for the
(association, approval_status)/(association, category) indexes this
leans on.

Architecture note — Candidate Eligibility (not implemented yet):
this module only ever answers "is eligible to vote", by design. It is
built as a plain "filters + base queryset -> narrowed queryset" pattern
specifically so a future `candidate_eligibility.py` (or a
`purpose="voting"|"candidacy"` parameter here) can reuse the exact same
filter fields and the exact same narrowing logic without any database
redesign — Election already has everything a candidacy rule would need
(scope + institution/faculty/department/level/gender/membership
category). Nothing in this module, or in how Election stores its
filters, assumes "eligible" can only ever mean "eligible to vote".
"""
from __future__ import annotations

import datetime

from django.utils import timezone

from apps.members.utils import latest_dob_for_age

# Minimum age to vote, applied to EVERY election (every scope, every
# filter combination, approved_members_only on or off). It is a rule about
# the voter, not a property of one kind of election, so it is deliberately
# a module constant here rather than an Election field -- changing it needs
# no schema change, and no election can be configured to opt out of it.
MINIMUM_VOTING_AGE = 18


def election_reference_date(election) -> datetime.date:
    """
    The calendar date an election's age rule is measured against: the
    local date (project TIME_ZONE) on which voting OPENS (start_datetime).

    One fixed date per election, rather than "today", so a member's
    eligibility cannot flip part-way through a multi-day voting window and
    an old election's eligible-voter count does not drift as time passes.
    A member who turns 18 on that date is eligible; one who turns 18 the
    next day is not.
    """
    start = election.start_datetime
    if timezone.is_aware(start):
        start = timezone.localtime(start)
    return start.date()


def minimum_age_dob_cutoff(election) -> datetime.date:
    """Latest date of birth that has reached MINIMUM_VOTING_AGE on the election's reference date."""
    return latest_dob_for_age(election_reference_date(election), MINIMUM_VOTING_AGE)


def eligible_members(election):
    """
    Returns a QuerySet of every Member eligible to vote in `election`,
    filtered entirely at the database level.

    Rules (all AND'ed together — a member must satisfy every filter that
    is actually set):

    - Always scoped to members of the election's own association.
    - approved_members_only (default True): member must be Approved and
      currently flagged voting_status=True (this single flag already
      encodes "approved and not suspended" — see Member.voting_status's
      own docstring in members/models.py).
    - Location/identity filters (institution, faculty, department,
      level, gender) are applied only when the election's scope is not
      National — a National election ignores them even if a stray value
      is present (see PROJECT BACKGROUND: "National Election: Ignore all
      filters").
    - Membership Category is the one exception: it applies regardless of
      scope, because National elections can still be restricted to
      Undergraduate-only or Alumni-only (see EXPECTED BEHAVIOUR:
      "National Election ... Undergraduate + Alumni / Undergraduate only
      / Alumni only").
    - A blank/unset filter is never applied — "not set" always means "no
      restriction from this filter", not "match nothing".
    - Minimum voting age (MINIMUM_VOTING_AGE = 18): the member must have
      reached 18 on the election's reference date (the day voting opens --
      see election_reference_date()), computed from the existing
      Member.date_of_birth. Applies to every election regardless of scope
      or any other setting. Done as one date_of_birth__lte filter, so a
      NULL/unknown date of birth never matches (fails closed).
    """
    from apps.members.models import Member

    from .models import Election

    qs = Member.objects.filter(association=election.association_id)

    if election.approved_members_only:
        qs = qs.filter(
            approval_status=Member.ApprovalStatus.APPROVED,
            voting_status=True,
        )

    if election.scope != Election.Scope.NATIONAL:
        if election.eligibility_institution:
            qs = qs.filter(institution__iexact=election.eligibility_institution)
        if election.eligibility_faculty:
            qs = qs.filter(faculty__iexact=election.eligibility_faculty)
        if election.eligibility_department:
            qs = qs.filter(department__iexact=election.eligibility_department)
        if election.eligibility_level:
            qs = qs.filter(level__iexact=election.eligibility_level)
        if election.eligibility_gender:
            qs = qs.filter(gender=election.eligibility_gender)

    if election.eligibility_membership_category:
        qs = qs.filter(category=election.eligibility_membership_category)

    # Minimum voting age -- unconditional (see MINIMUM_VOTING_AGE above).
    # Every other eligibility rule above is untouched; this only narrows
    # the result further, so no previously-ineligible member can become
    # eligible because of it.
    qs = qs.filter(date_of_birth__lte=minimum_age_dob_cutoff(election))

    return qs


def is_member_eligible(member, election) -> bool:
    """
    Whether a single `member` may vote in `election`. Reuses
    eligible_members() rather than duplicating the rule set — a single
    indexed EXISTS-style query, not a Python-side recomputation.
    """
    if member is None or election is None or member.association_id != election.association_id:
        return False
    return eligible_members(election).filter(pk=member.pk).exists()
