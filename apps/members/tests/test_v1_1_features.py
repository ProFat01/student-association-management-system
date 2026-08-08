"""
tests/test_v1_1_features.py — SAMS v1.1 feature tests

Covers:
  Feature 1 — Email field on Member (model, form, admin, portal)
  Feature 2 — Academic fields on registration form (faculty, dept, level)
  Feature 3 — Automatic approval email

Rules:
  - All existing tests remain untouched.
  - Every approval path tested: with email, without email, SMTP failure.
  - Approval MUST succeed even when SMTP fails.
  - No real SMTP calls are made (Django's locmem backend / mock).
"""
import logging
from unittest.mock import patch, MagicMock

from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.core.models import Association
from apps.members.models import Member, RegistrationApplication
from apps.members.email_service import send_approval_email, APPROVAL_EMAIL_SUBJECT

from .helpers import MediaIsolatedTestCase, make_image


# ---------------------------------------------------------------------------
# Helpers shared by this test module only
# ---------------------------------------------------------------------------

def _make_association(short_name="MSA"):
    return Association.objects.get_or_create(
        short_name=short_name,
        defaults={"name": "Malam Sidi Students Association", "slug": short_name.lower()},
    )[0]


def _make_member(association, phone="08099991111", nin="99991111111", email="", **extra):
    defaults = dict(
        association=association,
        full_name="Fatima Yusuf",
        phone_number=phone,
        nin_number=nin,
        date_of_birth="2000-06-15",
        institution="Malam Sidi College",
        course="Computer Science",
        category=Member.Category.UNDERGRADUATE,
        email=email,
    )
    defaults.update(extra)
    return Member.objects.create(passport_photo=make_image("p.png"), **defaults)


def _make_application(member):
    return RegistrationApplication.objects.create(member=member, receipt_image=make_image("r.png"))


# ---------------------------------------------------------------------------
# Feature 1 — Email field: model
# ---------------------------------------------------------------------------

class EmailFieldModelTests(MediaIsolatedTestCase):

    @classmethod
    def setUpTestData(cls):
        cls.association = _make_association()

    def test_email_defaults_to_empty_string(self):
        """Existing members that predate this field have email='' — never None."""
        member = _make_member(self.association)
        self.assertEqual(member.email, "")
        self.assertFalse(member.email)  # falsy, so 'if member.email' guards work

    def test_email_stored_and_retrieved(self):
        member = _make_member(self.association, email="fatima@example.com")
        member.refresh_from_db()
        self.assertEqual(member.email, "fatima@example.com")

    def test_email_optional_at_model_level(self):
        """Creating a member without email must not raise."""
        member = _make_member(self.association, phone="08011112222", nin="11112222333")
        member.full_clean()  # should not raise

    def test_email_field_validates_format(self):
        """An invalid email should fail full_clean."""
        from django.core.exceptions import ValidationError
        member = _make_member(self.association, phone="08011113333", nin="11113333444")
        member.email = "not-an-email"
        with self.assertRaises(ValidationError):
            member.full_clean()

    def test_existing_members_unaffected_after_migration(self):
        """
        Members created before the email field was added retain a blank
        email and remain fully valid — this simulates the live-system
        migration path.
        """
        member = _make_member(self.association, phone="08099998888", nin="99998888777")
        # email is blank by default — ensure approval still works
        application = _make_application(member)
        application.status = RegistrationApplication.Status.APPROVED
        application.save()
        member.refresh_from_db()
        self.assertEqual(member.approval_status, Member.ApprovalStatus.APPROVED)
        self.assertIsNotNone(member.membership_id)


# ---------------------------------------------------------------------------
# Feature 1 — Email field: registration form
# ---------------------------------------------------------------------------

@override_settings(DEFAULT_ASSOCIATION_SLUG="msa")
class EmailFieldRegistrationFormTests(MediaIsolatedTestCase):

    @classmethod
    def setUpTestData(cls):
        cls.association = Association.objects.create(
            name="Malam Sidi Students Association", short_name="MSA", slug="msa"
        )

    def _post_data(self, **overrides):
        data = {
            "full_name": "Bilal Hassan",
            "phone_number": "08055556666",
            "nin_number": "55556666777",
            "date_of_birth": "2001-03-20",
            "institution": "Malam Sidi College",
            "course": "Electrical Engineering",
            "category": Member.Category.UNDERGRADUATE,
            "passport_photo": make_image("photo.png"),
            "receipt_image": make_image("receipt.png"),
        }
        data.update(overrides)
        return data

    def test_registration_with_email_stores_email(self):
        self.client.post(
            reverse("members:register"),
            self._post_data(email="bilal@example.com"),
        )
        member = Member.objects.get(phone_number="08055556666")
        self.assertEqual(member.email, "bilal@example.com")

    def test_registration_without_email_succeeds(self):
        """Email is optional — omitting it must not block registration."""
        response = self.client.post(reverse("members:register"), self._post_data())
        self.assertEqual(response.status_code, 302)
        member = Member.objects.get(phone_number="08055556666")
        self.assertEqual(member.email, "")

    def test_registration_with_invalid_email_rejected(self):
        response = self.client.post(
            reverse("members:register"),
            self._post_data(email="not-valid-email"),
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Member.objects.filter(phone_number="08055556666").exists())

    def test_registration_form_shows_email_field(self):
        response = self.client.get(reverse("members:register"))
        self.assertContains(response, 'name="email"')


# ---------------------------------------------------------------------------
# Feature 2 — Academic fields on registration form
# ---------------------------------------------------------------------------

@override_settings(DEFAULT_ASSOCIATION_SLUG="msa")
class AcademicFieldsRegistrationTests(MediaIsolatedTestCase):

    @classmethod
    def setUpTestData(cls):
        cls.association = Association.objects.create(
            name="Malam Sidi Students Association", short_name="MSA", slug="msa"
        )

    def _post_data(self, **overrides):
        data = {
            "full_name": "Maryam Suleiman",
            "phone_number": "08077778888",
            "nin_number": "77778888999",
            "date_of_birth": "2003-09-12",
            "institution": "Malam Sidi College",
            "course": "Biology",
            "category": Member.Category.UNDERGRADUATE,
            "passport_photo": make_image("photo.png"),
            "receipt_image": make_image("receipt.png"),
        }
        data.update(overrides)
        return data

    def test_registration_form_shows_faculty_department_level_fields(self):
        response = self.client.get(reverse("members:register"))
        self.assertContains(response, 'name="faculty"')
        self.assertContains(response, 'name="department"')
        self.assertContains(response, 'name="level"')

    def test_academic_fields_stored_when_provided(self):
        self.client.post(
            reverse("members:register"),
            self._post_data(faculty="Science", department="Biological Sciences", level="200"),
        )
        member = Member.objects.get(phone_number="08077778888")
        self.assertEqual(member.faculty, "Science")
        self.assertEqual(member.department, "Biological Sciences")
        self.assertEqual(member.level, "200")

    def test_academic_fields_optional_registration_succeeds_without_them(self):
        response = self.client.post(reverse("members:register"), self._post_data())
        self.assertEqual(response.status_code, 302)
        member = Member.objects.get(phone_number="08077778888")
        self.assertEqual(member.faculty, "")
        self.assertEqual(member.department, "")
        self.assertEqual(member.level, "")

    def test_academic_fields_visible_in_portal_profile(self):
        """If a member has faculty/dept/level, the portal profile shows them."""
        member = _make_member(
            self.association,
            phone="08011110001", nin="11110001001",
            faculty="Engineering", department="Computer Eng.", level="300",
        )
        # Approve the member so they can log in to the portal
        application = _make_application(member)
        application.status = RegistrationApplication.Status.APPROVED
        application.save()
        member.refresh_from_db()

        # Log in to the portal via session
        session = self.client.session
        session["portal_member_id"] = member.pk
        session.save()

        response = self.client.get(reverse("members:portal_profile"))
        self.assertContains(response, "Engineering")
        self.assertContains(response, "Computer Eng.")
        self.assertContains(response, "300")

    def test_portal_profile_omits_empty_academic_fields(self):
        """Members without academic fields should not see empty dt/dd rows."""
        member = _make_member(self.association, phone="08011110002", nin="11110002002")
        application = _make_application(member)
        application.status = RegistrationApplication.Status.APPROVED
        application.save()
        member.refresh_from_db()

        session = self.client.session
        session["portal_member_id"] = member.pk
        session.save()

        response = self.client.get(reverse("members:portal_profile"))
        # The dt labels for missing academic fields should not appear
        self.assertNotContains(response, "<dt>Faculty</dt>")
        self.assertNotContains(response, "<dt>Department</dt>")
        self.assertNotContains(response, "<dt>Level</dt>")


# ---------------------------------------------------------------------------
# Feature 3 — Approval email: send_approval_email() unit tests
# ---------------------------------------------------------------------------

@override_settings(
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    DEFAULT_FROM_EMAIL="noreply@sams.test",
)
class ApprovalEmailServiceTests(MediaIsolatedTestCase):

    @classmethod
    def setUpTestData(cls):
        cls.association = _make_association(short_name="MSA2")

    def setUp(self):
        # Clear the test outbox before each test
        mail.outbox = []

    def _make_approved_member(self, email="", phone="08044441111", nin="44441111222"):
        member = _make_member(self.association, phone=phone, nin=nin, email=email)
        application = _make_application(member)
        # Approve via signal path — this also calls send_approval_email()
        # internally, but we test the service directly here too.
        application.status = RegistrationApplication.Status.APPROVED
        application.save()
        member.refresh_from_db()
        return member

    # --- Direct service calls (bypass signal, control email state) ---

    def test_send_approval_email_sends_one_email(self):
        member = self._make_approved_member(email="test@example.com", phone="08044441112", nin="44441112222")
        mail.outbox = []  # clear anything from signal
        result = send_approval_email(member)
        self.assertTrue(result)
        self.assertEqual(len(mail.outbox), 1)

    def test_send_approval_email_correct_recipient(self):
        member = self._make_approved_member(email="recipient@example.com", phone="08044441113", nin="44441113222")
        mail.outbox = []
        send_approval_email(member)
        self.assertEqual(mail.outbox[0].to, ["recipient@example.com"])

    def test_send_approval_email_correct_subject(self):
        member = self._make_approved_member(email="sub@example.com", phone="08044441114", nin="44441114222")
        mail.outbox = []
        send_approval_email(member)
        self.assertEqual(mail.outbox[0].subject, APPROVAL_EMAIL_SUBJECT)

    def test_send_approval_email_body_contains_membership_id(self):
        member = self._make_approved_member(email="body@example.com", phone="08044441115", nin="44441115222")
        mail.outbox = []
        send_approval_email(member)
        # Check both text and HTML bodies
        text_body = mail.outbox[0].body
        self.assertIn(member.membership_id, text_body)
        # HTML alternative
        html_body = mail.outbox[0].alternatives[0][0]
        self.assertIn(member.membership_id, html_body)

    def test_send_approval_email_body_contains_member_name(self):
        member = self._make_approved_member(email="name@example.com", phone="08044441116", nin="44441116222")
        mail.outbox = []
        send_approval_email(member)
        html_body = mail.outbox[0].alternatives[0][0]
        self.assertIn(member.full_name, html_body)

    def test_send_approval_email_body_contains_portal_and_card_links(self):
        member = self._make_approved_member(email="links@example.com", phone="08044441117", nin="44441117222")
        mail.outbox = []
        send_approval_email(member)
        text_body = mail.outbox[0].body
        self.assertIn("portal", text_body.lower())
        self.assertIn("card", text_body.lower())

    def test_send_approval_email_no_email_returns_false_sends_nothing(self):
        member = self._make_approved_member(phone="08044441118", nin="44441118222")  # no email
        mail.outbox = []
        result = send_approval_email(member)
        self.assertFalse(result)
        self.assertEqual(len(mail.outbox), 0)

    def test_send_approval_email_smtp_failure_returns_false_does_not_raise(self):
        """SMTP failure must be caught and logged, never raised."""
        member = self._make_approved_member(email="fail@example.com", phone="08044441119", nin="44441119222")
        member.refresh_from_db()
        mail.outbox = []

        with patch("apps.members.email_service.EmailMultiAlternatives.send", side_effect=Exception("SMTP down")):
            with self.assertLogs("apps.members.email_service", level="ERROR") as log_watcher:
                result = send_approval_email(member)

        self.assertFalse(result)
        self.assertTrue(any("Failed to send approval email" in msg for msg in log_watcher.output))

    def test_send_approval_email_has_html_alternative(self):
        """Email must be multipart with an HTML alternative."""
        member = self._make_approved_member(email="html@example.com", phone="08044441120", nin="44441120222")
        mail.outbox = []
        send_approval_email(member)
        self.assertEqual(len(mail.outbox[0].alternatives), 1)
        content_type = mail.outbox[0].alternatives[0][1]
        self.assertEqual(content_type, "text/html")


# ---------------------------------------------------------------------------
# Feature 3 — Approval email: integration via signal (the real code path)
# ---------------------------------------------------------------------------

@override_settings(
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    DEFAULT_FROM_EMAIL="noreply@sams.test",
)
class ApprovalEmailSignalIntegrationTests(MediaIsolatedTestCase):
    """
    Tests that confirm the email is sent (or not) through the normal
    approval workflow — i.e. via the signal, not a direct call to
    send_approval_email().
    """

    @classmethod
    def setUpTestData(cls):
        cls.association = _make_association(short_name="MSA3")

    def setUp(self):
        mail.outbox = []

    def test_approving_member_with_email_sends_email(self):
        member = _make_member(
            self.association, phone="08055550001", nin="55550001001", email="approved@example.com"
        )
        application = _make_application(member)
        application.status = RegistrationApplication.Status.APPROVED
        application.save()

        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["approved@example.com"])

    def test_approving_member_without_email_sends_no_email(self):
        member = _make_member(self.association, phone="08055550002", nin="55550002002")  # no email
        application = _make_application(member)
        application.status = RegistrationApplication.Status.APPROVED
        application.save()

        self.assertEqual(len(mail.outbox), 0)

    def test_approval_succeeds_even_when_smtp_fails(self):
        """
        This is the core safety guarantee: SMTP failure must NEVER roll back
        the approval or cause any exception to reach the admin.
        """
        member = _make_member(
            self.association, phone="08055550003", nin="55550003003", email="smtp-fail@example.com"
        )
        application = _make_application(member)

        with patch(
            "apps.members.email_service.EmailMultiAlternatives.send",
            side_effect=Exception("SMTP connection refused"),
        ):
            # This must not raise
            application.status = RegistrationApplication.Status.APPROVED
            application.save()

        member.refresh_from_db()
        # Approval went through correctly despite the SMTP failure
        self.assertEqual(member.approval_status, Member.ApprovalStatus.APPROVED)
        self.assertIsNotNone(member.membership_id)
        self.assertTrue(member.voting_status)

    def test_rejection_does_not_send_email(self):
        """Rejections must never trigger a welcome email."""
        member = _make_member(
            self.association, phone="08055550004", nin="55550004004", email="rejected@example.com"
        )
        application = _make_application(member)
        application.status = RegistrationApplication.Status.REJECTED
        application.rejection_reason = "Duplicate application."
        application.save()

        self.assertEqual(len(mail.outbox), 0)

    def test_re_saving_approved_application_does_not_send_duplicate_email(self):
        """
        Saving an already-approved application again (e.g. fixing a note)
        must not trigger a second email — the signal only fires on a real
        status *transition*, not on every save.
        """
        member = _make_member(
            self.association, phone="08055550005", nin="55550005005", email="once@example.com"
        )
        application = _make_application(member)
        application.status = RegistrationApplication.Status.APPROVED
        application.save()
        first_email_count = len(mail.outbox)

        application.save()  # no status change — should NOT fire again
        self.assertEqual(len(mail.outbox), first_email_count)

    def test_bulk_approve_action_sends_email_for_each_member_with_email(self):
        """
        The admin's bulk 'Approve selected applications' action goes through
        application.save() per application, so the signal fires for each.
        """
        members_with_email = []
        applications = []
        for i in range(3):
            m = _make_member(
                self.association,
                phone=f"0805556000{i}",
                nin=f"5556000{i}001",
                email=f"bulk{i}@example.com",
            )
            members_with_email.append(m)
            applications.append(_make_application(m))

        # One member without an email
        m_no_email = _make_member(self.association, phone="08055560099", nin="55560099001")
        applications.append(_make_application(m_no_email))

        for app in applications:
            app.status = RegistrationApplication.Status.APPROVED
            app.save()

        # Only the 3 members with email should receive one
        self.assertEqual(len(mail.outbox), 3)

    def test_email_sent_after_membership_id_is_assigned(self):
        """
        The welcome email body must contain the real membership_id.
        This verifies the signal calls refresh_from_db() before emailing
        so the ID written by Member.save() is present in the email.
        """
        member = _make_member(
            self.association, phone="08055550006", nin="55550006006", email="id-check@example.com"
        )
        application = _make_application(member)
        application.status = RegistrationApplication.Status.APPROVED
        application.save()

        member.refresh_from_db()
        self.assertIsNotNone(member.membership_id)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn(member.membership_id, mail.outbox[0].body)


# ---------------------------------------------------------------------------
# Feature 1 — Portal profile displays email
# ---------------------------------------------------------------------------

@override_settings(DEFAULT_ASSOCIATION_SLUG="msa")
class PortalProfileEmailDisplayTests(MediaIsolatedTestCase):

    @classmethod
    def setUpTestData(cls):
        cls.association = Association.objects.create(
            name="Malam Sidi Students Association", short_name="MSA", slug="msa"
        )

    def _login_portal(self, member):
        session = self.client.session
        session["portal_member_id"] = member.pk
        session.save()

    def test_portal_profile_shows_email_when_present(self):
        member = _make_member(
            self.association, phone="08033330001", nin="33330001001", email="visible@example.com"
        )
        application = _make_application(member)
        application.status = RegistrationApplication.Status.APPROVED
        application.save()
        member.refresh_from_db()
        self._login_portal(member)

        response = self.client.get(reverse("members:portal_profile"))
        self.assertContains(response, "visible@example.com")

    def test_portal_profile_omits_email_row_when_not_provided(self):
        member = _make_member(self.association, phone="08033330002", nin="33330002002")
        application = _make_application(member)
        application.status = RegistrationApplication.Status.APPROVED
        application.save()
        member.refresh_from_db()
        self._login_portal(member)

        response = self.client.get(reverse("members:portal_profile"))
        self.assertNotContains(response, "Email Address")
