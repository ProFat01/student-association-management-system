import os

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from apps.core.models import Association
from apps.members.models import Member, RegistrationApplication

from .helpers import MediaIsolatedTestCase, make_image


class ApprovalWorkflowTests(MediaIsolatedTestCase):
    @classmethod
    def setUpTestData(cls):
        cls.association = Association.objects.create(name="Malam Sidi Students Association", short_name="MSA")

    def _make_application(self, **member_overrides):
        defaults = dict(
            association=self.association, full_name="Test Member", phone_number="08012345678",
            nin_number="12345678901", date_of_birth="2001-01-01", institution="X", course="Y",
            category=Member.Category.UNDERGRADUATE,
        )
        defaults.update(member_overrides)
        member = Member.objects.create(passport_photo=make_image("p.png"), **defaults)
        application = RegistrationApplication.objects.create(member=member, receipt_image=make_image("r.png"))
        return member, application

    def test_approval_generates_membership_id_and_sets_eligibility(self):
        member, application = self._make_application()
        self.assertIsNone(member.membership_id)

        application.status = RegistrationApplication.Status.APPROVED
        application.save()
        member.refresh_from_db()

        self.assertEqual(member.approval_status, Member.ApprovalStatus.APPROVED)
        self.assertTrue(member.voting_status)
        self.assertIsNotNone(member.membership_id)
        self.assertTrue(member.membership_id.startswith("MSA-"))

    def test_membership_id_increments_across_members(self):
        member1, app1 = self._make_application(phone_number="08011111111", nin_number="11111111111")
        member2, app2 = self._make_application(phone_number="08022222222", nin_number="22222222222")

        app1.status = RegistrationApplication.Status.APPROVED
        app1.save()
        app2.status = RegistrationApplication.Status.APPROVED
        app2.save()

        member1.refresh_from_db()
        member2.refresh_from_db()
        self.assertNotEqual(member1.membership_id, member2.membership_id)

    def test_rejection_requires_a_reason(self):
        member, application = self._make_application()
        application.status = RegistrationApplication.Status.REJECTED
        with self.assertRaises(ValidationError) as ctx:
            application.full_clean(exclude=["application_number"])
        self.assertIn("rejection_reason", ctx.exception.message_dict)

    def test_rejection_with_reason_updates_member_and_blocks_voting(self):
        member, application = self._make_application()
        application.status = RegistrationApplication.Status.REJECTED
        application.rejection_reason = "Receipt unreadable."
        application.save()
        member.refresh_from_db()

        self.assertEqual(member.approval_status, Member.ApprovalStatus.REJECTED)
        self.assertFalse(member.voting_status)
        self.assertIsNone(member.membership_id)

    def test_receipt_deleted_from_disk_after_approval(self):
        member, application = self._make_application()
        receipt_path = application.receipt_image.path
        self.assertTrue(os.path.exists(receipt_path))

        application.status = RegistrationApplication.Status.APPROVED
        application.save()
        application.refresh_from_db()

        self.assertFalse(application.receipt_image)
        self.assertFalse(os.path.exists(receipt_path))

    def test_receipt_deleted_from_disk_after_rejection(self):
        member, application = self._make_application()
        receipt_path = application.receipt_image.path
        self.assertTrue(os.path.exists(receipt_path))

        application.status = RegistrationApplication.Status.REJECTED
        application.rejection_reason = "Duplicate payment receipt."
        application.save()
        application.refresh_from_db()

        self.assertFalse(application.receipt_image)
        self.assertFalse(os.path.exists(receipt_path))

    def test_editing_a_reviewed_application_again_does_not_re_trigger_member_sync(self):
        """
        Saving an already-approved application again (e.g. fixing a typo
        in an unrelated field) must not regenerate a second membership_id
        or re-run the receipt cleanup logic — the signal only acts on a
        genuine status *transition*, not every save.
        """
        member, application = self._make_application()
        application.status = RegistrationApplication.Status.APPROVED
        application.save()
        member.refresh_from_db()
        first_membership_id = member.membership_id

        application.save()  # status unchanged this time
        member.refresh_from_db()
        self.assertEqual(member.membership_id, first_membership_id)


class UniquenessConstraintTests(MediaIsolatedTestCase):
    """PART 8: no duplicate NIN/phone/membership_id/application_number — enforced at the DB level."""

    @classmethod
    def setUpTestData(cls):
        cls.association = Association.objects.create(name="Malam Sidi Students Association", short_name="MSA")

    def test_duplicate_phone_number_rejected_at_db_level(self):
        Member.objects.create(
            association=self.association, full_name="A", phone_number="08012345678",
            nin_number="11111111111", date_of_birth="2001-01-01", institution="X", course="Y",
            category=Member.Category.UNDERGRADUATE, passport_photo=make_image("a.png"),
        )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Member.objects.create(
                    association=self.association, full_name="B", phone_number="08012345678",
                    nin_number="22222222222", date_of_birth="2001-01-01", institution="X", course="Y",
                    category=Member.Category.UNDERGRADUATE, passport_photo=make_image("b.png"),
                )

    def test_duplicate_nin_rejected_at_db_level(self):
        Member.objects.create(
            association=self.association, full_name="A", phone_number="08011111111",
            nin_number="12345678901", date_of_birth="2001-01-01", institution="X", course="Y",
            category=Member.Category.UNDERGRADUATE, passport_photo=make_image("a.png"),
        )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Member.objects.create(
                    association=self.association, full_name="B", phone_number="08022222222",
                    nin_number="12345678901", date_of_birth="2001-01-01", institution="X", course="Y",
                    category=Member.Category.UNDERGRADUATE, passport_photo=make_image("b.png"),
                )

    def test_application_numbers_are_unique_and_sequential(self):
        member = Member.objects.create(
            association=self.association, full_name="A", phone_number="08011111111",
            nin_number="11111111111", date_of_birth="2001-01-01", institution="X", course="Y",
            category=Member.Category.UNDERGRADUATE, passport_photo=make_image("a.png"),
        )
        app1 = RegistrationApplication.objects.create(member=member, receipt_image=make_image("r1.png"))
        # simulate reapplication after a hypothetical rejection
        app2 = RegistrationApplication.objects.create(member=member, receipt_image=make_image("r2.png"))
        self.assertNotEqual(app1.application_number, app2.application_number)
        self.assertTrue(app1.application_number.startswith("APP-"))
        self.assertTrue(app2.application_number.startswith("APP-"))
