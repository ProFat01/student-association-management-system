"""
Minimum voting age (18) -- regression tests.

The rule lives in the Election Eligibility Engine
(apps/elections/eligibility.py) and is measured against the election's
reference date: the local calendar date on which voting opens. These tests
cover (1) the pure date maths, (2) the engine for every election scope,
(3) every layer that stops an under-18 member actually voting -- login,
direct ballot URL, an already-open voting session, a hand-built POST and
the model-level Vote.clean() -- (4) eligible-voter counts and analytics,
and (5) that ordinary adult voting and the one-vote-per-position
guarantee are exactly as they were.

No test here edits a Member's date of birth after a vote exists or alters
vote rows: members are created with the date of birth the scenario needs.
"""
import datetime
from itertools import count
from zoneinfo import ZoneInfo

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import Client, SimpleTestCase
from django.urls import reverse
from django.utils import timezone

from apps.analytics import services as analytics_services
from apps.core.models import Association
from apps.elections.eligibility import (
    MINIMUM_VOTING_AGE,
    eligible_members,
    election_reference_date,
    is_member_eligible,
    minimum_age_dob_cutoff,
)
from apps.elections.models import Candidate, Election, Position, Vote
from apps.elections.views import NOT_ELIGIBLE_MESSAGE, VOTING_SESSION_KEY_TEMPLATE
from apps.members.models import Member, RegistrationApplication
from apps.members.tests.helpers import MediaIsolatedTestCase, make_image
from apps.members.utils import calculate_age, latest_dob_for_age

LAGOS = ZoneInfo("Africa/Lagos")
D = datetime.date


# ---------------------------------------------------------------------------
# 1. Pure date maths
# ---------------------------------------------------------------------------
class CalculateAgeTests(SimpleTestCase):
    def test_age_on_the_day_before_the_birthday_is_one_less(self):
        self.assertEqual(calculate_age(D(2008, 6, 16), as_of=D(2026, 6, 15)), 17)

    def test_age_on_the_birthday_itself_counts(self):
        self.assertEqual(calculate_age(D(2008, 6, 15), as_of=D(2026, 6, 15)), 18)

    def test_age_the_day_after_the_birthday(self):
        self.assertEqual(calculate_age(D(2008, 6, 14), as_of=D(2026, 6, 15)), 18)

    def test_leap_day_birthday_is_reached_on_1_march_in_a_common_year(self):
        born = D(2008, 2, 29)
        self.assertEqual(calculate_age(born, as_of=D(2026, 2, 28)), 17)
        self.assertEqual(calculate_age(born, as_of=D(2026, 3, 1)), 18)

    def test_leap_day_birthday_is_reached_on_29_feb_in_a_leap_year(self):
        self.assertEqual(calculate_age(D(2008, 2, 29), as_of=D(2028, 2, 28)), 19)
        self.assertEqual(calculate_age(D(2008, 2, 29), as_of=D(2028, 2, 29)), 20)

    def test_defaults_to_today_in_local_time(self):
        born = D(2000, 1, 1)
        self.assertEqual(calculate_age(born), calculate_age(born, as_of=timezone.localdate()))


class LatestDobForAgeTests(SimpleTestCase):
    def test_simple_cutoff(self):
        self.assertEqual(latest_dob_for_age(D(2026, 6, 15), 18), D(2008, 6, 15))

    def test_cutoff_when_the_reference_day_is_29_feb(self):
        # 29 Feb 2028 minus 18 years has no 29 Feb: fall back to 28 Feb 2010.
        self.assertEqual(latest_dob_for_age(D(2028, 2, 29), 18), D(2010, 2, 28))

    def test_cutoff_agrees_with_calculate_age_for_every_day_across_leap_cycles(self):
        """Brute force: the DB-style cutoff and the Python age maths can never disagree."""
        day = D(2019, 1, 1)
        end = D(2031, 1, 1)
        checked = 0
        while day < end:
            cutoff = latest_dob_for_age(day, MINIMUM_VOTING_AGE)
            for offset in range(-4, 5):
                dob = cutoff + datetime.timedelta(days=offset)
                self.assertEqual(
                    calculate_age(dob, as_of=day) >= MINIMUM_VOTING_AGE,
                    dob <= cutoff,
                    msg=f"as_of={day} dob={dob} cutoff={cutoff}",
                )
                checked += 1
            day += datetime.timedelta(days=1)
        self.assertGreater(checked, 30000)


# ---------------------------------------------------------------------------
# Shared fixtures for the DB-backed tests
# ---------------------------------------------------------------------------
class AgeRuleTestCase(MediaIsolatedTestCase):
    @classmethod
    def setUpTestData(cls):
        cls.association = Association.objects.create(name="Malam Sidi Students Association", short_name="MSA")
        cls.president = Position.objects.create(association=cls.association, title="President", display_order=1)
        cls.secretary = Position.objects.create(association=cls.association, title="Secretary", display_order=2)

    def setUp(self):
        self._n = count(1)

    def _member(self, dob, approved=True, **overrides):
        n = next(self._n)
        defaults = dict(
            association=self.association, full_name=f"Voter {n}",
            phone_number=f"0801{n:07d}", nin_number=f"2{n:010d}", date_of_birth=dob,
            institution="Gombe State University", course="Computer Science",
            category=Member.Category.UNDERGRADUATE,
        )
        defaults.update(overrides)
        member = Member.objects.create(passport_photo=make_image("p.png"), **defaults)
        if approved:
            application = RegistrationApplication.objects.create(member=member)
            application.status = RegistrationApplication.Status.APPROVED
            application.save()
            member.refresh_from_db()
        return member

    def _election(self, start, end=None, **overrides):
        """An election opening at `start` (aware datetime). Not necessarily open *now*."""
        defaults = dict(
            association=self.association, name="Age Test Election",
            start_datetime=start, end_datetime=end or start + datetime.timedelta(hours=8),
        )
        defaults.update(overrides)
        election = Election(**defaults)
        election.full_clean()
        election.save()
        election.positions.set([self.president])
        return election

    def _open_election(self, **overrides):
        """An election that is open for voting right now but opened two days ago."""
        now = timezone.now()
        election = self._election(
            now - datetime.timedelta(days=2), now + datetime.timedelta(hours=2), **overrides
        )
        election.positions.set([self.president, self.secretary])
        Candidate.objects.create(election=election, position=self.president, name="Pres A")
        Candidate.objects.create(election=election, position=self.president, name="Pres B")
        Candidate.objects.create(election=election, position=self.secretary, name="Sec A")
        Candidate.objects.create(election=election, position=self.secretary, name="Sec B")
        return election

    @staticmethod
    def _dob_turning_18_on(reference, days_after=0):
        """DOB of someone who turns 18 `days_after` days after `reference` (negative = already 18)."""
        return latest_dob_for_age(reference, MINIMUM_VOTING_AGE) + datetime.timedelta(days=days_after)


# ---------------------------------------------------------------------------
# 2. Engine: boundaries, reference date, scopes
# ---------------------------------------------------------------------------
class AgeBoundaryTests(AgeRuleTestCase):
    """Election opens 15 June 2026 (10:00 Lagos): the reference date is 2026-06-15."""

    def setUp(self):
        super().setUp()
        self.election = self._election(datetime.datetime(2026, 6, 15, 10, 0, tzinfo=LAGOS))

    def test_reference_date_is_the_local_day_voting_opens(self):
        self.assertEqual(election_reference_date(self.election), D(2026, 6, 15))
        self.assertEqual(minimum_age_dob_cutoff(self.election), D(2008, 6, 15))

    def test_member_aged_17_is_not_eligible(self):
        member = self._member(D(2009, 6, 15))  # 17 on election day
        self.assertFalse(is_member_eligible(member, self.election))
        self.assertNotIn(member, eligible_members(self.election))

    def test_member_one_day_short_of_18_is_not_eligible(self):
        member = self._member(D(2008, 6, 16))  # turns 18 the day AFTER the election
        self.assertFalse(is_member_eligible(member, self.election))

    def test_member_turning_18_on_election_day_is_eligible(self):
        member = self._member(D(2008, 6, 15))
        self.assertTrue(is_member_eligible(member, self.election))

    def test_member_who_turned_18_the_day_before_is_eligible(self):
        member = self._member(D(2008, 6, 14))
        self.assertTrue(is_member_eligible(member, self.election))

    def test_adult_member_is_eligible(self):
        member = self._member(D(2001, 1, 1))
        self.assertTrue(is_member_eligible(member, self.election))

    def test_turning_18_after_the_election_stays_ineligible_even_if_18_today(self):
        """Measured on the election date, not today: this is how a CLOSED election's rules stay fixed."""
        member = self._member(D(2008, 7, 1))  # 18 on 1 Jul 2026, i.e. 16 days after this election
        self.assertFalse(is_member_eligible(member, self.election))

    def test_member_who_will_be_18_by_a_future_election_is_eligible_for_it(self):
        member = self._member(D(2010, 1, 1))  # 16 now-ish, but 18 on 2028-06-15
        future = self._election(datetime.datetime(2028, 6, 15, 10, 0, tzinfo=LAGOS))
        self.assertTrue(is_member_eligible(member, future))
        self.assertFalse(is_member_eligible(member, self.election))

    def test_reference_date_uses_local_time_not_utc(self):
        # 23:30 UTC on 14 June is 00:30 on 15 June in Lagos (UTC+1).
        election = self._election(datetime.datetime(2026, 6, 14, 23, 30, tzinfo=datetime.timezone.utc))
        self.assertEqual(election_reference_date(election), D(2026, 6, 15))
        self.assertTrue(is_member_eligible(self._member(D(2008, 6, 15)), election))
        # ...and 23:30 UTC on 15 June is already 16 June in Lagos.
        later = self._election(datetime.datetime(2026, 6, 15, 23, 30, tzinfo=datetime.timezone.utc))
        self.assertEqual(election_reference_date(later), D(2026, 6, 16))
        self.assertFalse(is_member_eligible(self._member(D(2008, 6, 17)), later))
        self.assertTrue(is_member_eligible(self._member(D(2008, 6, 16)), later))

    def test_leap_day_election_date(self):
        election = self._election(datetime.datetime(2028, 2, 29, 9, 0, tzinfo=LAGOS))
        self.assertTrue(is_member_eligible(self._member(D(2010, 2, 28)), election))
        self.assertFalse(is_member_eligible(self._member(D(2010, 3, 1)), election))

    def test_leap_day_birthday_member(self):
        leapling = self._member(D(2008, 2, 29))
        on_28th = self._election(datetime.datetime(2026, 2, 28, 9, 0, tzinfo=LAGOS))
        on_1st = self._election(datetime.datetime(2026, 3, 1, 9, 0, tzinfo=LAGOS))
        self.assertFalse(is_member_eligible(leapling, on_28th))
        self.assertTrue(is_member_eligible(leapling, on_1st))

    def test_unapproved_adult_is_still_ineligible_existing_rule_preserved(self):
        member = self._member(D(2001, 1, 1), approved=False)
        self.assertFalse(is_member_eligible(member, self.election))

    def test_suspended_adult_is_still_ineligible_existing_rule_preserved(self):
        member = self._member(D(2001, 1, 1))
        member.voting_status = False
        member.save(update_fields=["voting_status"])
        self.assertFalse(is_member_eligible(member, self.election))


class AgeRuleAcrossElectionScopesTests(AgeRuleTestCase):
    """The age rule must hold for every scope while every other scope rule keeps working."""

    START = datetime.datetime(2026, 6, 15, 10, 0, tzinfo=LAGOS)
    ADULT = D(2001, 1, 1)
    MINOR = D(2009, 6, 15)  # 17 on election day

    def _assert_age_rule(self, election, **matching):
        """`matching` = Member fields that satisfy the election's own filters."""
        adult = self._member(self.ADULT, **matching)
        minor = self._member(self.MINOR, **matching)
        eligible = set(eligible_members(election).values_list("pk", flat=True))
        self.assertIn(adult.pk, eligible, "adult matching the scope must stay eligible")
        self.assertNotIn(minor.pk, eligible, "under-18 must be excluded")
        self.assertTrue(is_member_eligible(adult, election))
        self.assertFalse(is_member_eligible(minor, election))
        return adult, minor

    def test_national_election(self):
        self._assert_age_rule(self._election(self.START, scope=Election.Scope.NATIONAL))

    def test_national_election_with_membership_category_filter(self):
        election = self._election(
            self.START, scope=Election.Scope.NATIONAL,
            eligibility_membership_category=Member.Category.UNDERGRADUATE,
        )
        self._assert_age_rule(election, category=Member.Category.UNDERGRADUATE)

    def test_institution_election(self):
        election = self._election(
            self.START, scope=Election.Scope.INSTITUTION, eligibility_institution="Gombe State University",
        )
        adult, _ = self._assert_age_rule(election, institution="Gombe State University")
        elsewhere = self._member(self.ADULT, institution="Other University")
        self.assertFalse(is_member_eligible(elsewhere, election), "institution rule must still apply")

    def test_faculty_election(self):
        election = self._election(
            self.START, scope=Election.Scope.FACULTY,
            eligibility_institution="Gombe State University", eligibility_faculty="Science",
        )
        self._assert_age_rule(election, faculty="Science")

    def test_department_election(self):
        election = self._election(
            self.START, scope=Election.Scope.DEPARTMENT, eligibility_institution="Gombe State University",
            eligibility_faculty="Science", eligibility_department="Computer Science",
        )
        self._assert_age_rule(election, faculty="Science", department="Computer Science")

    def test_level_representative_election(self):
        election = self._election(self.START, scope=Election.Scope.CUSTOM, eligibility_level="200")
        adult, _ = self._assert_age_rule(election, level="200")
        wrong_level = self._member(self.ADULT, level="300")
        self.assertFalse(is_member_eligible(wrong_level, election), "level rule must still apply")

    def test_female_representative_election(self):
        election = self._election(
            self.START, scope=Election.Scope.CUSTOM, eligibility_gender=Member.Gender.FEMALE,
        )
        adult, _ = self._assert_age_rule(election, gender=Member.Gender.FEMALE)
        male = self._member(self.ADULT, gender=Member.Gender.MALE)
        self.assertFalse(is_member_eligible(male, election), "gender rule must still apply")

    def test_age_rule_applies_even_when_approved_members_only_is_off(self):
        election = self._election(self.START, approved_members_only=False)
        pending_minor = self._member(self.MINOR, approved=False)
        pending_adult = self._member(self.ADULT, approved=False)
        self.assertNotIn(pending_minor, eligible_members(election))
        self.assertIn(pending_adult, eligible_members(election))


# ---------------------------------------------------------------------------
# 3. Every voting layer
# ---------------------------------------------------------------------------
class AgeRuleVotingLayersTests(AgeRuleTestCase):
    def setUp(self):
        super().setUp()
        self.election = self._open_election()
        ref = election_reference_date(self.election)
        self.minor = self._member(self._dob_turning_18_on(ref, days_after=1))  # 17 on election day
        self.adult = self._member(self._dob_turning_18_on(ref, days_after=0))  # exactly 18 on election day
        self.login_url = reverse("elections:voting_login", args=[self.election.pk])
        self.ballot_url = reverse("elections:ballot", args=[self.election.pk])
        self.success_url = reverse("elections:vote_success", args=[self.election.pk])
        self.session_key = VOTING_SESSION_KEY_TEMPLATE.format(election_id=self.election.pk)

    def _login(self, member):
        return self.client.post(self.login_url, {
            "method": "membership_id", "membership_id": member.membership_id, "phone_number": member.phone_number,
        })

    def _inject_session(self, member):
        """Simulates a voting session that already exists for `member` (e.g. opened before the rule)."""
        session = self.client.session
        session[self.session_key] = member.pk
        session.save()

    def _ballot_payload(self):
        return {
            f"position_{self.president.pk}": self.election.candidates.filter(position=self.president).first().pk,
            f"position_{self.secretary.pk}": self.election.candidates.filter(position=self.secretary).first().pk,
        }

    # -- login ------------------------------------------------------------
    def test_underage_member_cannot_log_in_to_vote(self):
        response = self._login(self.minor)
        self.assertEqual(response.status_code, 200)  # form re-shown, no redirect to the ballot
        self.assertContains(response, NOT_ELIGIBLE_MESSAGE)
        self.assertNotIn(self.session_key, self.client.session)

    def test_login_message_does_not_reveal_age_or_date_of_birth(self):
        response = self._login(self.minor)
        self.assertNotContains(response, "18 years")
        self.assertNotContains(response, "under 18")
        self.assertNotContains(response, "too young")
        self.assertNotContains(response, self.minor.date_of_birth.isoformat())

    def test_member_turning_18_on_election_day_can_log_in_and_reach_ballot(self):
        response = self._login(self.adult)
        self.assertRedirects(response, self.ballot_url)
        self.assertEqual(self.client.session[self.session_key], self.adult.pk)

    # -- direct ballot URL ------------------------------------------------
    def test_direct_ballot_url_without_a_session_redirects_to_login(self):
        response = self.client.get(self.ballot_url)
        self.assertRedirects(response, self.login_url)

    def test_direct_ballot_post_without_a_session_records_nothing(self):
        response = self.client.post(self.ballot_url, self._ballot_payload())
        self.assertRedirects(response, self.login_url)
        self.assertEqual(Vote.objects.count(), 0)

    # -- an existing voting session --------------------------------------
    def test_existing_session_for_underage_member_cannot_view_ballot(self):
        self._inject_session(self.minor)
        response = self.client.get(self.ballot_url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, NOT_ELIGIBLE_MESSAGE)
        self.assertNotContains(response, "Pres A")  # the ballot itself is never rendered
        self.assertNotIn(self.session_key, self.client.session)  # stale session is destroyed

    def test_existing_session_stops_working_when_member_is_not_eligible(self):
        self._inject_session(self.minor)
        self.client.get(self.ballot_url)
        # The session is gone, so the next request is simply sent back to the login page.
        self.assertRedirects(self.client.get(self.ballot_url), self.login_url)

    # -- manipulated POST requests ---------------------------------------
    def test_manipulated_post_from_underage_session_records_no_votes(self):
        self._inject_session(self.minor)
        response = self.client.post(self.ballot_url, self._ballot_payload())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, NOT_ELIGIBLE_MESSAGE)
        self.assertEqual(Vote.objects.filter(member=self.minor).count(), 0)
        self.assertEqual(Vote.objects.count(), 0)

    def test_repeated_manipulated_posts_still_record_nothing(self):
        for _ in range(3):
            self._inject_session(self.minor)
            self.client.post(self.ballot_url, self._ballot_payload())
        self.assertEqual(Vote.objects.count(), 0)

    # -- model layer (any path that skips the view) -----------------------
    def test_vote_model_validation_rejects_underage_member(self):
        candidate = self.election.candidates.filter(position=self.president).first()
        vote = Vote(election=self.election, member=self.minor, candidate=candidate)
        with self.assertRaises(ValidationError) as ctx:
            vote.full_clean()
        self.assertIn("member", ctx.exception.message_dict)

    def test_vote_model_validation_accepts_member_turning_18_on_election_day(self):
        candidate = self.election.candidates.filter(position=self.president).first()
        Vote(election=self.election, member=self.adult, candidate=candidate).full_clean()  # does not raise

    # -- ordinary voting is unchanged ------------------------------------
    def test_eligible_member_can_complete_a_full_ballot(self):
        self._login(self.adult)
        response = self.client.post(self.ballot_url, self._ballot_payload())
        self.assertRedirects(response, self.success_url)
        self.assertEqual(Vote.objects.filter(member=self.adult).count(), 2)  # one per position

    def test_eligible_member_cannot_vote_twice_through_the_view(self):
        self._login(self.adult)
        self.client.post(self.ballot_url, self._ballot_payload())
        # A second attempt with a fresh login is sent straight to the success page.
        response = self._login(self.adult)
        self.assertRedirects(response, self.success_url)
        self.assertEqual(Vote.objects.filter(member=self.adult).count(), 2)

    def test_one_vote_per_position_database_constraint_still_holds(self):
        candidates = list(self.election.candidates.filter(position=self.president))
        Vote.objects.create(election=self.election, member=self.adult, candidate=candidates[0])
        with self.assertRaises(IntegrityError), transaction.atomic():
            Vote.objects.create(election=self.election, member=self.adult, candidate=candidates[1])

    def test_underage_and_adult_voting_in_the_same_election(self):
        """Two separate browsers: the under-18's attempt records nothing and the adult's is unaffected."""
        minor_browser, adult_browser = Client(), Client()
        for browser, member in ((minor_browser, self.minor), (adult_browser, self.adult)):
            browser.post(self.login_url, {
                "method": "membership_id", "membership_id": member.membership_id, "phone_number": member.phone_number,
            })
            browser.post(self.ballot_url, self._ballot_payload())
        self.assertEqual(Vote.objects.filter(member=self.minor).count(), 0)
        self.assertEqual(Vote.objects.filter(member=self.adult).count(), 2)


# ---------------------------------------------------------------------------
# 4. Counts, turnout, analytics, historical records
# ---------------------------------------------------------------------------
class AgeRuleCountsAndAnalyticsTests(AgeRuleTestCase):
    START = datetime.datetime(2026, 6, 15, 10, 0, tzinfo=LAGOS)

    def setUp(self):
        super().setUp()
        self.election = self._election(self.START)
        self.adults = [self._member(D(2001, 1, 1)), self._member(D(2008, 6, 15)), self._member(D(1995, 3, 3))]
        self.minors = [self._member(D(2009, 6, 15)), self._member(D(2008, 6, 16))]

    def test_eligible_voter_count_excludes_underage_members(self):
        self.assertEqual(self.election.eligible_voters_count(), 3)
        self.assertEqual(self.election.eligible_members().count(), 3)

    def test_turnout_uses_the_age_filtered_denominator(self):
        candidate = Candidate.objects.create(election=self.election, position=self.president, name="C")
        Vote.objects.create(election=self.election, member=self.adults[0], candidate=candidate)
        self.assertEqual(self.election.turnout_percentage(), 33.3)  # 1 of 3 adults, not 1 of 5 members

    def test_results_page_shows_age_filtered_eligible_count(self):
        response = self.client.get(reverse("elections:results", args=[self.election.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["eligible_voters_count"], 3)

    def test_analytics_election_overview_uses_the_same_engine(self):
        overview = analytics_services.election_overview(self.election)
        self.assertEqual(overview["eligible_voters"], 3)
        self.assertEqual(overview["eligible_undergraduate"], 3)

    def test_age_participation_does_not_count_underage_members_as_eligible(self):
        rows = {r["bracket"]: r for r in analytics_services.age_participation(self.election, as_of=D(2026, 6, 15))}
        # 3 adults aged 25, 18 and 31 -> brackets 21_25, 16_20, 31_40; the two 17-year-olds are not counted.
        self.assertEqual(sum(r["eligible"] for r in rows.values()), 3)
        self.assertEqual(rows["16_20"]["eligible"], 1)  # only the 18-year-old, not the two 17-year-olds
        self.assertEqual(rows["below_16"]["eligible"], 0)

    def test_historical_votes_are_untouched_and_results_still_render(self):
        """A vote already on record from a member who is under 18 on the election date is never edited or removed."""
        candidate = Candidate.objects.create(election=self.election, position=self.president, name="C")
        recorded = Vote.objects.create(election=self.election, member=self.minors[0], candidate=candidate)

        self.assertEqual(Vote.objects.count(), 1)
        self.assertTrue(Vote.objects.filter(pk=recorded.pk).exists())
        self.assertEqual(self.election.voters_count(), 1)          # the historical ballot is still counted
        self.assertEqual(self.election.eligible_voters_count(), 3)  # the denominator excludes them

        results = self.election.results_by_position()
        self.assertEqual(results[0]["total_votes"], 1)
        response = self.client.get(reverse("elections:results", args=[self.election.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Vote.objects.count(), 1)

    def test_checking_eligibility_never_modifies_member_or_election_records(self):
        before_members = list(Member.objects.order_by("pk").values())
        before_election = Election.objects.filter(pk=self.election.pk).values().get()
        self.election.eligible_voters_count()
        self.election.turnout_percentage()
        analytics_services.election_overview(self.election)
        analytics_services.age_participation(self.election)
        self.assertEqual(before_members, list(Member.objects.order_by("pk").values()))
        self.assertEqual(before_election, Election.objects.filter(pk=self.election.pk).values().get())
