import datetime

from django.utils import timezone

from apps.core.utils import get_next_sequence


def generate_membership_id(association) -> str:
    """
    Format: <SHORT_NAME>-<YEAR>-<0001>
    e.g. MSA-2026-0001

    Only called once a RegistrationApplication is approved (see
    members/signals.py) — unapproved members never get a membership_id,
    which is itself a cheap extra guard against an unapproved record
    being mistaken for a real member anywhere downstream.
    """
    year = timezone.now().year
    n = get_next_sequence(association, "membership_id")
    return f"{association.short_name}-{year}-{n:04d}"


def generate_application_number(association) -> str:
    """
    Format: APP-<YEAR>-<00001>
    e.g. APP-2026-00001

    Note: this intentionally drops the association short_name that the
    very first version of this helper included (APP-MSA-2026-00001) —
    the registration module's spec calls for the shorter
    APP-YYYY-00001 form explicitly. The counter itself is still scoped
    per-association internally (get_next_sequence(association, ...)), so
    today, with a single association, this is exactly equivalent and the
    change is purely cosmetic. The real-world consequence to flag: if a
    second association is ever onboarded, both associations' counters
    independently restart, so two different associations could each mint
    "APP-2026-00001" in the same year — and `application_number` is
    globally unique, so the second one to save would fail outright. If
    multi-tenancy actually launches, reintroducing the association code
    into this string (or moving to a single global counter) is the fix —
    flagged here rather than silently risking it.
    """
    year = timezone.now().year
    n = get_next_sequence(association, "application_number")
    return f"APP-{year}-{n:05d}"


def calculate_age(date_of_birth, as_of=None) -> int:
    """
    Whole years between `date_of_birth` and `as_of` (default: today in the
    project's local timezone). Computed on demand and never stored, so the
    value is always current -- there is no age column on Member to go stale.

    A birthday counts as reached ON the birthday itself (someone born
    2008-06-15 is 18 on 2026-06-15, not the day after). A 29 February
    birthday is treated as reached on 1 March in non-leap years, i.e. the
    person has not yet turned a year older on 28 February. This is the
    same (month, day) tuple comparison apps/analytics/services.py already
    uses for its age brackets, so the two can never disagree.
    """
    as_of = as_of or timezone.localdate()
    return as_of.year - date_of_birth.year - ((as_of.month, as_of.day) < (date_of_birth.month, date_of_birth.day))


def latest_dob_for_age(as_of, years) -> datetime.date:
    """
    The most recent date of birth that has ALREADY reached `years` on
    `as_of`: anyone born on or before the returned date is `years` or older
    on `as_of`; anyone born after it is younger. Lets age rules run as a
    plain indexed `date_of_birth__lte=` database filter instead of looping
    over members in Python.

    Equivalent to `calculate_age(dob, as_of) >= years` for every date --
    including the 29 February edge cases: when `as_of` is itself 29 Feb the
    same-day-N-years-ago does not exist, so 28 Feb is used (a person born
    on 28 Feb turns `years` on 28 Feb; one born on 29 Feb or 1 Mar has not
    yet, which matches calculate_age()'s tuple comparison above).
    """
    try:
        return as_of.replace(year=as_of.year - years)
    except ValueError:  # as_of is 29 Feb and the target year is not a leap year
        return as_of.replace(year=as_of.year - years, day=28)
