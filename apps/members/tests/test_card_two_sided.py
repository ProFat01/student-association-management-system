"""
Two-sided Membership Identity Card -- tests.

Covers: front and back rendering, the automatically calculated age (never
stored, never the date of birth), unchanged QR generation / verification /
permissions / public-page privacy, and that both faces are present in the
print output at the CR80 size. Existing behaviour is exercised again here
on purpose so a regression in the redesign cannot slip through.
"""
import datetime
import re
from pathlib import Path
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.management import call_command
from django.test import RequestFactory, SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from apps.core.models import Association, SiteSettings
from apps.members.card_services import build_verification_url, generate_qr_png
from apps.members.models import Member, MembershipCard
from apps.members.utils import calculate_age

from .helpers import MediaIsolatedTestCase
from .test_card import _make_approved_member

User = get_user_model()
D = datetime.date


def _fake_today(day):
    """Make the age calculation believe today is `day` (the card must re-compute on every view)."""
    return patch("apps.members.utils.timezone.localdate", return_value=day)


@override_settings(DEFAULT_ASSOCIATION_SLUG="msa")
class TwoSidedCardTestCase(MediaIsolatedTestCase):
    @classmethod
    def setUpTestData(cls):
        cls.association = Association.objects.create(
            name="Malam Sidi Students Association", short_name="MSA", slug="msa"
        )

    def _login_portal(self, member):
        session = self.client.session
        session["portal_member_id"] = member.pk
        session.save()

    def _card_page(self, member=None, **member_overrides):
        member = member or _make_approved_member(self.association, **member_overrides)
        self._login_portal(member)
        response = self.client.get(reverse("members:portal_card"))
        self.assertEqual(response.status_code, 200)
        return member, response


# ---------------------------------------------------------------------------
# Front and back rendering
# ---------------------------------------------------------------------------
class FrontAndBackRenderingTests(TwoSidedCardTestCase):
    def test_page_has_exactly_one_front_and_one_back_face(self):
        _, response = self._card_page()
        html = response.content.decode()
        self.assertEqual(len(re.findall(r'class="id-card id-card-front', html)), 1)
        self.assertEqual(len(re.findall(r'class="id-card id-card-back', html)), 1)

    def test_faces_are_labelled_front_and_back_on_screen(self):
        _, response = self._card_page()
        self.assertContains(response, 'id="card-front-label">Front<')
        self.assertContains(response, 'id="card-back-label">Back<')

    def test_front_shows_the_members_identity_details(self):
        member, response = self._card_page(
            faculty="Science", department="Computer Science", level="300", gender="female"
        )
        for expected in (
            "Malam Sidi Students Association", "Membership Identity Card", member.full_name,
            member.membership_id, "Science", "Computer Science", "300", "Female", "Scan to verify",
            "Date of issue", "Approved", "Signature",
        ):
            self.assertContains(response, expected)

    def test_front_omits_blank_optional_fields_rather_than_printing_empty_labels(self):
        _, response = self._card_page()  # no faculty / department / level / gender
        for label in (">Faculty<", ">Department<", ">Level<", ">Gender<"):
            self.assertNotContains(response, label)

    def test_back_uses_msa_terminology_and_no_national_id_wording(self):
        _, response = self._card_page()
        html = response.content.decode()
        back = html[html.index("id-card-back"):]
        for expected in (
            "identifies the holder as a member of the Malam Sidi Students Association (MSA)",
            "MSA Administration", "Holder&rsquo;s responsibilities", "Verification and conditions",
            "Scan the QR code on the front",
        ):
            self.assertIn(expected, back)
        for forbidden in ("Federal", "National Identity", "NIN", "Nigerian", "NIMC", "citizen"):
            self.assertNotIn(forbidden, html)

    def test_back_shows_membership_id(self):
        member, response = self._card_page()
        html = response.content.decode()
        self.assertIn(member.membership_id, html[html.index("id-card-back-footer"):])

    def test_back_contact_line_uses_only_existing_site_settings(self):
        SiteSettings.objects.create(
            association=self.association, contact_email="info@msa-example.org", contact_phone="08030000000"
        )
        _, response = self._card_page()
        html = response.content.decode()
        self.assertIn("info@msa-example.org", html[html.index("id-card-back-footer"):])
        self.assertContains(response, "08030000000")

    def test_back_invents_no_contact_details_when_none_are_configured(self):
        _, response = self._card_page()  # no SiteSettings row at all
        self.assertNotContains(response, "id-card-back-contact")
        html = response.content.decode()
        back_footer = html[html.index("id-card-back-footer"):html.index("</section>", html.index("id-card-back-footer"))]
        self.assertNotIn("@", back_footer)  # no email address (the page <head> legitimately has "@" in a font URL)

    def test_member_without_photo_still_renders_both_faces(self):
        member = _make_approved_member(self.association)
        member.passport_photo = ""
        member.save(update_fields=["passport_photo"])
        _, response = self._card_page(member)
        self.assertContains(response, "id-card-photo-placeholder")
        self.assertContains(response, "id-card-back")

    def test_unusually_long_text_switches_to_the_compact_scale(self):
        _, long_page = self._card_page(
            full_name="Abdulrahman Muhammad Sani Ibrahim Yerima", phone_number="08099999999", nin_number="99999999999",
            membership_id="MSA-0099",
        )
        self.assertContains(long_page, "id-card-front id-card-compact")
        self.client.session.flush()

    def test_normal_text_uses_the_regular_scale(self):
        _, response = self._card_page()
        self.assertNotContains(response, "id-card-compact")

    def test_staff_view_shows_both_faces_too(self):
        call_command("setup_roles", verbosity=0)
        member = _make_approved_member(self.association)
        user = User.objects.create_user(username="reg_admin", password="x", is_staff=True)
        user.groups.add(Group.objects.get(name="Registration Admin"))
        self.client.login(username="reg_admin", password="x")
        response = self.client.get(reverse("members:staff_card", args=[member.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "id-card-front")
        self.assertContains(response, "id-card-back")
        self.assertContains(response, reverse("members:staff_card_qr", args=[member.pk]))


# ---------------------------------------------------------------------------
# Automatic age
# ---------------------------------------------------------------------------
class CardAgeTests(TwoSidedCardTestCase):
    def test_card_shows_the_age_calculated_from_date_of_birth(self):
        # _make_approved_member uses DOB 2002-05-14
        with _fake_today(D(2026, 11, 25)):
            _, response = self._card_page()
        self.assertContains(response, "<dt>Age</dt><dd>24 years</dd>", html=False)

    def test_age_changes_when_the_date_changes(self):
        member = _make_approved_member(self.association)
        self._login_portal(member)
        with _fake_today(D(2026, 5, 13)):  # the day before the 24th birthday
            before = self.client.get(reverse("members:portal_card"))
        with _fake_today(D(2026, 5, 14)):  # the birthday itself
            on_day = self.client.get(reverse("members:portal_card"))
        with _fake_today(D(2027, 5, 14)):  # a year later
            next_year = self.client.get(reverse("members:portal_card"))
        self.assertContains(before, "<dd>23 years</dd>")
        self.assertContains(on_day, "<dd>24 years</dd>")
        self.assertContains(next_year, "<dd>25 years</dd>")

    def test_age_is_not_stored_anywhere(self):
        member_fields = {f.name for f in Member._meta.get_fields()}
        card_fields = {f.name for f in MembershipCard._meta.get_fields()}
        self.assertNotIn("age", member_fields)
        self.assertNotIn("age", card_fields)
        self.assertFalse([name for name in member_fields | card_fields if "age" == name.split("_")[-1] and name != "image"])

    def test_viewing_the_card_does_not_modify_the_member_record(self):
        member = _make_approved_member(self.association)
        before = Member.objects.filter(pk=member.pk).values().get()
        self._card_page(member)
        self.assertEqual(before, Member.objects.filter(pk=member.pk).values().get())

    def test_age_for_a_member_who_turns_18_today(self):
        self.assertEqual(calculate_age(D(2008, 6, 15), as_of=D(2026, 6, 15)), 18)

    def test_singular_year_is_grammatical(self):
        # Not a realistic member, but the template must never print "1 years".
        member = _make_approved_member(self.association, date_of_birth=D(2025, 6, 1))
        with _fake_today(D(2026, 11, 25)):
            _, response = self._card_page(member)
        self.assertContains(response, "<dd>1 year</dd>")

    def test_card_never_prints_the_date_of_birth_or_other_sensitive_fields(self):
        member, response = self._card_page()
        for forbidden in (
            "2002-05-14", "14 May 2002", "May 14, 2002", "14/05/2002", "05/14/2002",  # every common DOB format
            member.nin_number, member.phone_number,
        ):
            self.assertNotContains(response, forbidden)


# ---------------------------------------------------------------------------
# QR, verification, permissions and privacy are unchanged
# ---------------------------------------------------------------------------
class UnchangedCardBehaviourTests(TwoSidedCardTestCase):
    def test_qr_endpoint_still_returns_a_png(self):
        member = _make_approved_member(self.association)
        self._login_portal(member)
        response = self.client.get(reverse("members:portal_card_qr"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "image/png")
        self.assertTrue(response.content.startswith(b"\x89PNG\r\n\x1a\n"))

    def test_qr_generation_is_the_existing_local_generator(self):
        self.assertTrue(generate_qr_png("https://example.com/members/verify/x/").startswith(b"\x89PNG"))

    def test_card_page_uses_the_existing_qr_endpoint_and_no_third_party_qr_service(self):
        _, response = self._card_page()
        self.assertContains(response, reverse("members:portal_card_qr"))
        html = response.content.decode()
        for host in ("api.qrserver.com", "chart.googleapis.com", "qrcode.tec-it", "goqr.me"):
            self.assertNotIn(host, html)

    def test_redesign_does_not_change_the_card_uuid_or_verification_url(self):
        member = _make_approved_member(self.association)
        card = MembershipCard.get_or_create_for(member)
        uuid_before = card.card_uuid
        expected_url = build_verification_url(RequestFactory().get("/"), card)

        self._card_page(member)
        self._card_page(member)

        card.refresh_from_db()
        self.assertEqual(card.card_uuid, uuid_before)
        self.assertEqual(MembershipCard.objects.filter(member=member).count(), 1)
        self.assertEqual(build_verification_url(RequestFactory().get("/"), card), expected_url)
        self.assertIn(f"/members/verify/{uuid_before}/", expected_url)

    def test_existing_verification_still_validates_and_stays_private(self):
        member = _make_approved_member(self.association)
        card = MembershipCard.get_or_create_for(member)
        response = self.client.get(reverse("members:verify_member", args=[card.card_uuid]))
        self.assertContains(response, "Valid Member")
        self.assertContains(response, member.full_name)
        for forbidden in (member.nin_number, member.phone_number, str(member.date_of_birth), "14 May 2002"):
            self.assertNotContains(response, forbidden)
        self.assertNotRegex(response.content.decode(), r"\b\d{2} years\b")  # no age either

    def test_unapproved_card_still_verifies_as_invalid(self):
        member = _make_approved_member(self.association, approval_status=Member.ApprovalStatus.PENDING)
        card = MembershipCard.get_or_create_for(member)
        response = self.client.get(reverse("members:verify_member", args=[card.card_uuid]))
        self.assertContains(response, "Invalid Membership")
        self.assertNotContains(response, member.full_name)

    def test_anonymous_visitor_is_still_sent_to_login(self):
        response = self.client.get(reverse("members:portal_card"))
        self.assertRedirects(response, reverse("members:portal_login"))

    def test_rejected_member_still_loses_card_access(self):
        member = _make_approved_member(self.association, approval_status=Member.ApprovalStatus.REJECTED)
        self._login_portal(member)
        self.assertRedirects(self.client.get(reverse("members:portal_card")), reverse("members:portal_login"))

    def test_a_member_only_ever_sees_their_own_card(self):
        mine = _make_approved_member(self.association)
        other = _make_approved_member(
            self.association, full_name="Zainab Other", phone_number="08099990000", nin_number="55555555555",
            membership_id="MSA-0777",
        )
        _, response = self._card_page(mine)
        self.assertContains(response, mine.full_name)
        self.assertNotContains(response, other.full_name)
        self.assertNotContains(response, other.membership_id)

    def test_staff_permissions_are_unchanged(self):
        call_command("setup_roles", verbosity=0)
        member = _make_approved_member(self.association)
        User.objects.create_user(username="plain", password="x", is_staff=True)
        self.client.login(username="plain", password="x")
        self.assertEqual(self.client.get(reverse("members:staff_card", args=[member.pk])).status_code, 403)
        self.client.logout()
        self.assertEqual(self.client.get(reverse("members:staff_card", args=[member.pk])).status_code, 302)


# ---------------------------------------------------------------------------
# Print output
# ---------------------------------------------------------------------------
CARD_CSS = Path(settings.BASE_DIR) / "static" / "css" / "card.css"


class PrintOutputTests(TwoSidedCardTestCase):
    def test_both_faces_sit_inside_the_one_printable_sheet(self):
        _, response = self._card_page()
        html = response.content.decode()
        sheet = html[html.index('class="card-sheet"'):html.index('class="card-actions')]
        self.assertIn("id-card-front", sheet)
        self.assertIn("id-card-back", sheet)
        self.assertLess(sheet.index("id-card-front"), sheet.index("id-card-back"))  # page 1 = front, page 2 = back

    def test_screen_only_chrome_is_marked_no_print(self):
        _, response = self._card_page()
        html = response.content.decode()
        for marker in ('card-side-label no-print', 'card-title no-print', 'card-actions no-print', 'card-print-help no-print'):
            self.assertIn(marker, html)

    def test_print_help_explains_two_sided_printing(self):
        _, response = self._card_page()
        self.assertContains(response, "How to print both sides")
        self.assertContains(response, "flip on long edge")
        self.assertContains(response, "100% (actual size)")


class PrintStylesheetTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        css = CARD_CSS.read_text()
        cls.css = css
        cls.print_css = css[css.index("@media print"):]

    def test_both_faces_are_locked_to_cr80_size(self):
        self.assertIn("width: 85.6mm;", self.print_css)
        self.assertIn("height: 53.98mm;", self.print_css)

    def test_front_and_back_print_on_separate_pages(self):
        self.assertIn(".card-side-front", self.print_css)
        self.assertIn("break-after: page;", self.print_css)
        self.assertIn("break-inside: avoid;", self.print_css)

    def test_nothing_above_the_card_can_shift_one_page_relative_to_the_other(self):
        self.assertIn(".card-view", self.print_css)
        self.assertRegex(self.print_css, r"\.card-view\s*\{[^}]*padding:\s*0;")

    def test_navigation_and_controls_are_hidden_in_print(self):
        for selector in (".site-header", ".site-footer", ".card-actions", ".no-print"):
            self.assertIn(selector, self.print_css)
        self.assertIn("display: none !important;", self.print_css)

    def test_backgrounds_are_kept_when_printing(self):
        self.assertIn("print-color-adjust: exact;", self.print_css)

    def test_card_artwork_is_sized_relative_to_the_card_not_the_screen(self):
        """No viewport units inside the face: that is what keeps screen and print identical."""
        card_rules = self.css[self.css.index(".id-card {"):self.css.index("@media print")]
        self.assertNotRegex(card_rules, r"\d(vw|vh)\b")

    def test_both_faces_share_the_cr80_ratio_on_screen(self):
        self.assertIn("aspect-ratio: 85.6 / 53.98;", self.css)


class ExistingMobilePrintingTests(TwoSidedCardTestCase):
    def test_print_button_and_mobile_dialog_hooks_are_all_still_present(self):
        _, response = self._card_page()
        for hook in ('id="print-card-btn"', 'id="mobile-print-dialog"', 'id="mobile-print-dismiss"', 'id="mobile-print-anyway"'):
            self.assertContains(response, hook)
        self.assertContains(response, "Print / Save as PDF")
        self.assertContains(response, "js/card.js")

    def test_card_script_is_unchanged_and_still_wires_those_hooks(self):
        js = (Path(settings.BASE_DIR) / "static" / "js" / "card.js").read_text()
        for hook in ("print-card-btn", "mobile-print-dialog", "mobile-print-dismiss", "mobile-print-anyway"):
            self.assertIn(hook, js)


class NoSchemaChangeTests(TestCase):
    def test_no_migration_is_required(self):
        """Both enhancements are computed on demand; neither may add or alter a column."""
        call_command("makemigrations", "--check", "--dry-run", verbosity=0)  # raises SystemExit if a migration is needed
