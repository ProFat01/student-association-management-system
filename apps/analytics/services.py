"""
Business logic for the analytics module. Views (dashboards and JSON API)
call into these functions and either render a template or json.dumps the
result — none of this logic lives in views.py itself, and none of it
lives on Member/Election (both already-completed modules this task
isn't allowed to rewrite).

Two computation styles on purpose:
  - membership/course/institution/age/growth: always computed live from
    Member directly. These are cheap GROUP BY/COUNT queries even at
    several thousand members, and "accurate" matters more here than
    "cached" — see ELECTION_MODULE.md's identical reasoning for live
    results over snapshots.
  - Snapshot *generation* functions at the bottom populate the existing
    MembershipSnapshot / AgeDistributionSnapshot / ElectionResultSnapshot
    tables on demand (PART 9) — for historical trend tracking and as the
    "future optimization" path the spec asks to leave open, not because
    today's dashboards depend on them.
"""
from collections import Counter

from django.db import transaction
from django.utils import timezone

from apps.elections.models import Election

from .models import AgeDistributionSnapshot, ElectionResultSnapshot, MembershipSnapshot
from .querysets import course_counts, institution_counts, members_for_association, registration_counts_by_period


def _percentage(part: int, whole: int) -> float:
    return round(part / whole * 100, 1) if whole else 0.0


def _calculate_age(date_of_birth, as_of=None) -> int:
    as_of = as_of or timezone.now().date()
    return as_of.year - date_of_birth.year - ((as_of.month, as_of.day) < (date_of_birth.month, date_of_birth.day))


# ---------------------------------------------------------------------------
# PART 1: Membership analytics
# ---------------------------------------------------------------------------
def membership_overview(association) -> dict:
    from apps.members.models import Member

    members = members_for_association(association)
    total = members.count()
    approved = members.filter(approval_status=Member.ApprovalStatus.APPROVED).count()
    pending = members.filter(approval_status=Member.ApprovalStatus.PENDING).count()
    rejected = members.filter(approval_status=Member.ApprovalStatus.REJECTED).count()
    undergraduate = members.filter(category=Member.Category.UNDERGRADUATE).count()
    alumni = members.filter(alumni_status=True).count()

    return {
        "total_members": total,
        "total_approved": approved,
        "approved_percentage": _percentage(approved, total),
        "total_pending": pending,
        "pending_percentage": _percentage(pending, total),
        "total_rejected": rejected,
        "rejected_percentage": _percentage(rejected, total),
        "total_undergraduate": undergraduate,
        "undergraduate_percentage": _percentage(undergraduate, total),
        "total_alumni": alumni,
        "alumni_percentage": _percentage(alumni, total),
    }


# ---------------------------------------------------------------------------
# PARTS 2 & 3: Course / Institution analytics — same shape, same helper
# ---------------------------------------------------------------------------
def _distribution(rows, label_field, order="desc") -> list:
    rows = list(rows)
    total = sum(row["count"] for row in rows)
    for row in rows:
        row["percentage"] = _percentage(row["count"], total)
    # Two stable passes: alphabetical-by-label first, then by count — ties
    # come out in a deterministic, readable order instead of DB-dependent
    # GROUP BY ordering.
    rows.sort(key=lambda row: row[label_field])
    rows.sort(key=lambda row: row["count"], reverse=(order != "asc"))
    return rows


def course_distribution(association, order="desc") -> list:
    """order: "desc" (highest membership first, default) or "asc" (lowest first)."""
    return _distribution(course_counts(association), "course", order)


def institution_distribution(association, order="desc") -> list:
    return _distribution(institution_counts(association), "institution", order)


# ---------------------------------------------------------------------------
# PART 4: Age analytics
# ---------------------------------------------------------------------------
def age_distribution(association, as_of=None) -> list:
    as_of = as_of or timezone.now().date()
    dobs = members_for_association(association).exclude(date_of_birth__isnull=True).values_list(
        "date_of_birth", flat=True
    )
    bucket_counts = Counter(AgeDistributionSnapshot.bucket_for_age(_calculate_age(dob, as_of)) for dob in dobs)
    total = sum(bucket_counts.values())

    return [
        {
            "bracket": bracket_value,
            "label": bracket_label,
            "count": bucket_counts.get(bracket_value, 0),
            "percentage": _percentage(bucket_counts.get(bracket_value, 0), total),
        }
        for bracket_value, bracket_label in AgeDistributionSnapshot.AgeBracket.choices
    ]


# ---------------------------------------------------------------------------
# PART 5: Registration growth
# ---------------------------------------------------------------------------
_GROWTH_LABEL_FORMATS = {"day": "%d %b %Y", "month": "%B %Y", "year": "%Y"}


def registration_growth(association, granularity="month") -> list:
    """
    granularity: "day" | "month" | "year". Returns chronologically
    ordered [{"period": date, "label": "January 2026", "count": N}, ...]
    — a plain list of plain dicts, already shaped for a future chart
    library to consume directly (PART 5's "helper methods for future
    chart integration") without that library needing to know anything
    about Django querysets or Trunc functions.
    """
    if granularity not in _GROWTH_LABEL_FORMATS:
        raise ValueError(f"granularity must be one of {list(_GROWTH_LABEL_FORMATS)}, got {granularity!r}")

    rows = sorted(registration_counts_by_period(association, granularity), key=lambda row: row["period"])
    label_format = _GROWTH_LABEL_FORMATS[granularity]
    return [
        {
            "period": row["period"],
            "label": row["period"].strftime(label_format) if row["period"] else "Unknown",
            "count": row["count"],
        }
        for row in rows
    ]


# ---------------------------------------------------------------------------
# PART 6: Election analytics
# ---------------------------------------------------------------------------
def election_overview(election) -> dict:
    """Eligible/cast/turnout already exist on Election itself (built in the election module) — this just packages them with position/candidate counts."""
    return {
        "election": election,
        "eligible_voters": election.eligible_voters_count(),
        "votes_cast": election.voters_count(),
        "turnout_percentage": election.turnout_percentage(),
        "total_positions": election.positions.count(),
        "total_candidates": election.candidates.count(),
    }


def all_elections_overview(association) -> list:
    return [election_overview(election) for election in Election.objects.filter(association=association)]


# ---------------------------------------------------------------------------
# PART 7: Position analytics (vote totals, percentages, winner)
# ---------------------------------------------------------------------------
def position_results_with_winner(election) -> list:
    """
    Builds on Election.results_by_position() (already-approved election
    module code, untouched here) and adds winner determination on top,
    rather than teaching the elections app about "winners" — that's an
    analytics-module concern, not something Election itself needs to
    know how to compute.
    """
    results = election.results_by_position()
    for item in results:
        candidates = item["candidates"]  # already ordered by -vote_count, name
        if not candidates or item["total_votes"] == 0:
            item["winner"] = None
            item["is_tie"] = False
            continue

        top_count = candidates[0]["vote_count"]
        leaders = [row["candidate"] for row in candidates if row["vote_count"] == top_count]
        if len(leaders) > 1:
            item["winner"] = None
            item["is_tie"] = True
            item["tied_candidates"] = leaders
        else:
            item["winner"] = leaders[0]
            item["is_tie"] = False
    return results


# ---------------------------------------------------------------------------
# PART 8: Age participation analytics
# ---------------------------------------------------------------------------
def age_participation(election, as_of=None) -> list:
    as_of = as_of or timezone.now().date()
    eligible_members = (
        members_for_association(election.association)
        .filter(voting_status=True)
        .exclude(date_of_birth__isnull=True)
        .values_list("id", "date_of_birth")
    )
    voted_member_ids = set(election.votes.values_list("member_id", flat=True).distinct())

    eligible_counts = Counter()
    voted_counts = Counter()
    for member_id, date_of_birth in eligible_members:
        bracket = AgeDistributionSnapshot.bucket_for_age(_calculate_age(date_of_birth, as_of))
        eligible_counts[bracket] += 1
        if member_id in voted_member_ids:
            voted_counts[bracket] += 1

    return [
        {
            "bracket": bracket_value,
            "label": bracket_label,
            "eligible": eligible_counts.get(bracket_value, 0),
            "voted": voted_counts.get(bracket_value, 0),
            "participation_percentage": _percentage(
                voted_counts.get(bracket_value, 0), eligible_counts.get(bracket_value, 0)
            ),
        }
        for bracket_value, bracket_label in AgeDistributionSnapshot.AgeBracket.choices
    ]


# ---------------------------------------------------------------------------
# PART 9: Snapshot generation — populates the existing snapshot models
# ---------------------------------------------------------------------------
@transaction.atomic
def generate_membership_snapshot(association, snapshot_date=None) -> MembershipSnapshot:
    snapshot_date = snapshot_date or timezone.now().date()
    overview = membership_overview(association)
    snapshot, _created = MembershipSnapshot.objects.update_or_create(
        association=association,
        snapshot_date=snapshot_date,
        defaults={
            "total_members": overview["total_members"],
            "total_approved": overview["total_approved"],
            "total_pending": overview["total_pending"],
            "total_rejected": overview["total_rejected"],
            "total_alumni": overview["total_alumni"],
            "total_undergraduate": overview["total_undergraduate"],
        },
    )
    return snapshot


@transaction.atomic
def generate_age_distribution_snapshot(association, snapshot_date=None) -> list:
    snapshot_date = snapshot_date or timezone.now().date()
    rows = age_distribution(association, as_of=snapshot_date)
    snapshots = []
    for row in rows:
        snapshot, _created = AgeDistributionSnapshot.objects.update_or_create(
            association=association,
            snapshot_date=snapshot_date,
            age_bracket=row["bracket"],
            defaults={"count": row["count"]},
        )
        snapshots.append(snapshot)
    return snapshots


@transaction.atomic
def generate_election_result_snapshots(election) -> list:
    """
    Refreshes the numbers/winner on the existing ElectionResultSnapshot
    rows for every contested position — deliberately leaves
    `is_published` untouched (defaults to False only on first creation).
    Generating/refreshing a snapshot is not the same action as publishing
    it; that stays the explicit, audited admin action the election module
    already built.
    """
    results = position_results_with_winner(election)
    eligible = election.eligible_voters_count()
    turnout = election.turnout_percentage()
    snapshots = []
    for item in results:
        snapshot, _created = ElectionResultSnapshot.objects.update_or_create(
            election=election,
            position=item["position"],
            defaults={
                "total_votes_cast": item["total_votes"],
                "total_eligible_voters": eligible,
                "turnout_percentage": turnout,
                "winner_candidate": item["winner"],
            },
        )
        snapshots.append(snapshot)
    return snapshots
