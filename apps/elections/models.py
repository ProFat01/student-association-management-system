"""
apps.elections.

Ballot structure: one Election explicitly declares which Positions it
contests (`Election.positions`, a M2M to Position), each Position can
have multiple Candidates within that election, and a Vote is cast for one
candidate within one position. The integrity guarantee — "an approved
member can vote at most once per position, during the election's voting
window" — is enforced at the database level via a UniqueConstraint on
(election, member, position) on Vote, not just in application code.

(Earlier iteration note, kept for history: the very first version of
this app modeled "one vote per election" with no position field at all,
i.e. one Election = one single contested office. That was flagged at the
time as the wrong shape for a combined multi-position ballot and has now
been superseded by the design below.)
"""
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone

from apps.core.models import Association


class Position(models.Model):
    """
    A contestable office (President, General Secretary, ...), normalized
    out of free-text so the same position can be compared/reported on
    consistently across multiple elections and associations — a plain
    CharField on Candidate would let "President", "president", and
    "Pres." all exist as different strings and quietly break analytics.

    Positions belong to an Association, not to a single Election, so the
    same "President" position can be reused election after election
    (clean year-over-year reporting) — which Elections actually contest
    a given Position is recorded on Election.positions below.
    """

    association = models.ForeignKey(
        Association, on_delete=models.CASCADE, related_name="positions"
    )
    title = models.CharField(max_length=100)
    description = models.TextField(blank=True)
    display_order = models.PositiveSmallIntegerField(
        default=0, help_text="Controls ordering on ballots/results, lowest first."
    )

    class Meta:
        ordering = ["display_order", "title"]
        constraints = [
            models.UniqueConstraint(fields=["association", "title"], name="unique_position_per_association")
        ]
        verbose_name = "Position"
        verbose_name_plural = "Positions"

    def __str__(self):
        return self.title


class Election(models.Model):
    association = models.ForeignKey(
        Association, on_delete=models.PROTECT, related_name="elections"
    )
    name = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    # The explicit "contains multiple positions" relationship: defines the
    # ballot structure (which offices are being contested) independently
    # of candidates being nominated yet. Candidate.clean() below requires
    # a candidate's position to be one of these, so the ballot shape is
    # decided first and candidates are added against it — not inferred
    # after the fact from whatever candidates happen to exist.
    positions = models.ManyToManyField(
        Position,
        related_name="elections",
        blank=True,
        help_text="Positions being contested in this election. Add these before adding candidates.",
    )
    start_datetime = models.DateTimeField()
    end_datetime = models.DateTimeField()
    # Admin-controlled switch, independent of the clock: lets staff publish
    # an election (visible, candidates locked in) ahead of its voting
    # window, or pull one down in an emergency, without touching the
    # start/end timestamps. Whether voting is *actually* open right now is
    # always computed from the clock (see is_active()/is_voting_open
    # below), never this flag alone.
    #
    # Named `is_enabled` rather than `is_active` specifically because the
    # election-module spec requires a method called `is_active()` that
    # means something different (currently between start_datetime and
    # end_datetime) — Python won't let a field and a method share one
    # name on the same class, so one of the two had to be renamed. This
    # field keeps its exact original behaviour, just under a name that
    # doesn't collide; nothing about what it *does* changed.
    is_enabled = models.BooleanField(
        default=True, help_text="Whether this election is published/enabled at all."
    )
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_elections",
    )

    class Meta:
        ordering = ["-start_datetime"]
        permissions = [
            ("manage_election", "Can create, edit, or activate/deactivate elections"),
            ("publish_results", "Can publish election results"),
        ]
        verbose_name = "Election"
        verbose_name_plural = "Elections"

    def __str__(self):
        return self.name

    def clean(self):
        if self.start_datetime and self.end_datetime and self.end_datetime <= self.start_datetime:
            raise ValidationError({"end_datetime": "End time must be after the start time."})

    # --- Status: computed on every access, never stored. ---------------
    # Storing a "status" column that has to be kept in sync with the clock
    # is exactly the kind of staleness this project avoids elsewhere (see
    # analytics.AgeDistributionSnapshot's reasoning for the same call on
    # age). Computing it fresh on every access is what "status should
    # update automatically" means here: there is no cron job, no signal,
    # and nothing that can fall out of sync, because nothing is ever
    # cached in the first place.
    def is_upcoming(self) -> bool:
        return timezone.now() < self.start_datetime

    def is_active(self) -> bool:
        """Between start and end, by the clock alone — independent of is_enabled."""
        now = timezone.now()
        return self.start_datetime <= now <= self.end_datetime

    def is_closed(self) -> bool:
        return timezone.now() > self.end_datetime

    @property
    def status(self) -> str:
        """One of "upcoming" / "active" / "closed" — for display in templates/admin."""
        if self.is_upcoming():
            return "upcoming"
        if self.is_closed():
            return "closed"
        return "active"

    @property
    def is_voting_open(self) -> bool:
        """
        The single source of truth for "can a member actually vote right
        now" — combines the admin's publish/enable switch with the pure
        clock-based is_active() check above. Use this (not is_active()
        alone) anywhere voting eligibility is being decided; is_active()
        alone answers a narrower question ("are we inside the time
        window") that an admin can still override by disabling the
        election.
        """
        return bool(self.is_enabled and self.is_active())

    # --- Voting / results helpers, all computed live. -------------------
    def has_member_voted(self, member) -> bool:
        return self.votes.filter(member=member).exists()

    def eligible_voters_count(self) -> int:
        """Members of this election's association currently flagged eligible to vote."""
        return self.association.members.filter(voting_status=True).count()

    def voters_count(self) -> int:
        """Distinct members who have cast at least one vote — i.e. ballots submitted, not Vote rows."""
        return self.votes.values("member_id").distinct().count()

    def turnout_percentage(self) -> float:
        eligible = self.eligible_voters_count()
        if not eligible:
            return 0.0
        return round(self.voters_count() / eligible * 100, 1)

    def results_by_position(self):
        """
        Live vote tally for every contested position: each candidate's
        vote count and share-of-vote percentage (of votes cast *for that
        position*, the standard election-results meaning — not of total
        eligible voters, which is what turnout_percentage() answers
        instead). Computed fresh from Vote on every call — no caching, no
        snapshot table, deliberately (see PART 8 in ELECTION_MODULE.md
        for why this module doesn't reuse analytics.ElectionResultSnapshot).
        """
        results = []
        for position in self.positions.all().order_by("display_order", "title"):
            candidates = list(
                self.candidates.filter(position=position)
                .annotate(vote_count=models.Count("votes"))
                .order_by("-vote_count", "name")
            )
            total = sum(c.vote_count for c in candidates)
            rows = [
                {
                    "candidate": c,
                    "vote_count": c.vote_count,
                    "percentage": round(c.vote_count / total * 100, 1) if total else 0.0,
                }
                for c in candidates
            ]
            results.append({"position": position, "candidates": rows, "total_votes": total})
        return results


class Candidate(models.Model):
    election = models.ForeignKey(Election, on_delete=models.CASCADE, related_name="candidates")
    position = models.ForeignKey(Position, on_delete=models.PROTECT, related_name="candidates")
    name = models.CharField(max_length=255)
    photo = models.ImageField(upload_to="elections/candidates/%Y/%m/", blank=True, null=True)
    manifesto = models.TextField(blank=True)

    class Meta:
        ordering = ["position__display_order", "name"]
        constraints = [
            # "Candidate cannot appear twice for same position in same
            # election" — this is about blocking an accidental *duplicate
            # entry* (the same name added twice for President in this
            # election), not about limiting how many different candidates
            # can contest one position, which is the entire point of an
            # election. Re-using a name for the same position in a
            # *different* election (someone running again next year) is
            # still allowed.
            models.UniqueConstraint(
                fields=["election", "position", "name"], name="unique_candidate_name_per_position_per_election"
            )
        ]
        verbose_name = "Candidate"
        verbose_name_plural = "Candidates"

    def __str__(self):
        return f"{self.name} — {self.position}"

    def clean(self):
        if self.position_id and self.election_id:
            if self.position.association_id != self.election.association_id:
                raise ValidationError(
                    {"position": "Position must belong to the same association as the election."}
                )
            # self.pk check: an unsaved Candidate can't query its own M2M
            # membership via self.election.positions (the election side is
            # fine either way; this guards the case where election itself
            # is also unsaved, e.g. validating a bound but unsaved form).
            if self.election_id and not self.election.positions.filter(pk=self.position_id).exists():
                raise ValidationError(
                    {"position": "This position is not contested in the selected election. Add it to the election's positions first."}
                )


class Vote(models.Model):
    election = models.ForeignKey(Election, on_delete=models.PROTECT, related_name="votes")
    member = models.ForeignKey(
        "members.Member", on_delete=models.PROTECT, related_name="votes"
    )
    candidate = models.ForeignKey(Candidate, on_delete=models.PROTECT, related_name="votes")
    # Denormalized from candidate.position, deliberately. A DB-level
    # UniqueConstraint can only reference columns that actually live on
    # this table — it can't reach through candidate.position — so the
    # position is copied onto Vote itself (auto-assigned in clean()/save(),
    # never set by hand) purely so "one vote per member per position" can
    # be a real database constraint instead of an application-level check
    # that a bug or a race could slip past.
    position = models.ForeignKey(
        Position, on_delete=models.PROTECT, related_name="votes", editable=False
    )
    timestamp = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-timestamp"]
        constraints = [
            # The core integrity guarantee: an approved member can cast at
            # most one vote per position within a given election. Two
            # concurrent requests racing to vote for the same member/
            # election/position can't both succeed — the second INSERT
            # fails this constraint rather than racing past an
            # application-level check.
            models.UniqueConstraint(
                fields=["election", "member", "position"], name="unique_vote_per_member_per_position"
            )
        ]
        verbose_name = "Vote"
        verbose_name_plural = "Votes"

    def __str__(self):
        return f"{self.member} -> {self.candidate} ({self.position})"

    def _assign_position(self):
        """Keep `position` in lockstep with `candidate.position` — never set independently."""
        if self.candidate_id:
            self.position_id = self.candidate.position_id

    def clean(self):
        self._assign_position()
        if self.candidate_id and self.election_id and self.candidate.election_id != self.election_id:
            raise ValidationError({"candidate": "Candidate does not belong to the selected election."})
        if self.member_id and not self.member.voting_status:
            raise ValidationError({"member": "This member is not currently eligible to vote."})
        if self.election_id and not self.election.is_voting_open:
            raise ValidationError({"election": "Voting is not currently open for this election."})

    def save(self, *args, **kwargs):
        self._assign_position()
        super().save(*args, **kwargs)

