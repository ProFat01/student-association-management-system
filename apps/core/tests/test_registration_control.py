"""
Tests for the membership registration open/close control
(SiteSettings.registration_open + core.manage_registration_status).

Layout
------
* RegistrationSwitchModelTests -- the stored setting and its helper
* PublicRegistrationTests      -- what visitors and existing members see
* AdminRegistrationControlTests -- who may change it, and how
"""
from django.contrib.auth.models import Group, Permission
from django.core.management import call_command
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from apps.accounts.models import (
    ROLE_ANALYTICS_ADMIN,
    ROLE_ELECTION_ADMIN,
    ROLE_REGISTRATION_ADMIN,
    ROLE_SUPER_ADMIN,
    User,
)
from apps.core.models import Association, SiteSettings
from apps.core.utils import is_registration_open
from apps.members.models import Member, RegistrationApplication
from apps.members.services import BY_MEMBERSHIP_ID
from apps.members.tests.helpers import MediaIsolatedTestCase, make_image

PERMISSION = "core.manage_registration_status"


def _registration_post_data(**overrides):
    data = {
        "full_name": "Aisha Bello",
        "phone_number": "08012345678",
        "nin_number": "12345678901",
        "date_of_birth": "2002-05-14",
        "gender": Member.Gender.FEMALE,
        "institution": "Gombe State University (GSU), Tudun Wada",
        "course": "Computer Science",
        "category": Member.Category.UNDERGRADUATE,
        "passport_photo": make_image("photo.png"),
        "receipt_image": make_image("receipt.png"),
    }
    data.update(overrides)
    return data


class RegistrationSwitchModelTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.association = Association.objects.create(
            name="Malam Sidi Students Association", short_name="MSA", slug="msa"
        )

    def test_registration_is_open_by_default(self):
        settings_row = SiteSettings.objects.create(association=self.association)
        self.assertTrue(settings_row.registration_open)

    def test_association_without_a_settings_row_counts_as_open(self):
        self.assertFalse(SiteSettings.objects.exists())
        self.assertTrue(is_registration_open(self.association))

    def test_helper_follows_the_stored_value_every_time(self):
        settings_row = SiteSettings.objects.create(association=self.association)
        self.assertTrue(is_registration_open(self.association))

        settings_row.registration_open = False
        settings_row.save()
        self.assertFalse(is_registration_open(self.association))

        SiteSettings.objects.filter(pk=settings_row.pk).update(registration_open=True)
        self.assertTrue(is_registration_open(self.association))

    def test_setting_is_stored_in_the_database(self):
        SiteSettings.objects.create(association=self.association, registration_open=False)
        fresh = SiteSettings.objects.get(association=self.association)
        self.assertFalse(fresh.registration_open)


@override_settings(DEFAULT_ASSOCIATION_SLUG="msa")
class PublicRegistrationTests(MediaIsolatedTestCase):
    @classmethod
    def setUpTestData(cls):
        cls.association = Association.objects.create(
            name="Malam Sidi Students Association", short_name="MSA", slug="msa"
        )

    def set_open(self, is_open):
        SiteSettings.objects.update_or_create(
            association=self.association, defaults={"registration_open": is_open}
        )

    def test_registration_works_when_open_without_any_settings_row(self):
        response = self.client.get(reverse("members:register"))
        self.assertContains(response, 'name="full_name"')
        self.assertNotContains(response, "Registration is currently closed")

        response = self.client.post(reverse("members:register"), _registration_post_data())
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Member.objects.count(), 1)

    def test_registration_works_when_explicitly_open(self):
        self.set_open(True)
        response = self.client.post(reverse("members:register"), _registration_post_data())
        self.assertEqual(response.status_code, 302)
        self.assertEqual(RegistrationApplication.objects.count(), 1)

    def test_closed_registration_shows_a_professional_message_and_no_form(self):
        self.set_open(False)
        response = self.client.get(reverse("members:register"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Registration is currently closed")
        self.assertContains(response, "not accepting new membership applications")
        self.assertNotContains(response, 'name="full_name"')
        self.assertNotContains(response, "Submit Registration")

    def test_closed_registration_rejects_a_submission_and_creates_nothing(self):
        self.set_open(False)
        response = self.client.post(reverse("members:register"), _registration_post_data())
        self.assertEqual(response.status_code, 403)
        self.assertContains(response, "Registration is currently closed", status_code=403)
        self.assertFalse(Member.objects.exists())
        self.assertFalse(RegistrationApplication.objects.exists())

    def test_reopening_allows_applications_again(self):
        self.set_open(False)
        self.assertEqual(
            self.client.post(reverse("members:register"), _registration_post_data()).status_code, 403
        )
        self.assertFalse(Member.objects.exists())

        self.set_open(True)
        response = self.client.get(reverse("members:register"))
        self.assertContains(response, 'name="full_name"')
        response = self.client.post(reverse("members:register"), _registration_post_data())
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Member.objects.count(), 1)
        self.assertEqual(RegistrationApplication.objects.count(), 1)

    def test_closing_only_affects_the_public_application_form(self):
        self.set_open(False)
        for url_name in ("core:home", "members:status_check", "members:portal_login", "elections:election_list"):
            with self.subTest(url_name=url_name):
                self.assertEqual(self.client.get(reverse(url_name)).status_code, 200)

    def test_closed_page_still_links_to_status_check_and_member_login(self):
        self.set_open(False)
        response = self.client.get(reverse("members:register"))
        self.assertContains(response, reverse("members:status_check"))
        self.assertContains(response, reverse("members:portal_login"))

    def test_existing_member_can_still_log_in_while_registration_is_closed(self):
        member = Member.objects.create(
            association=self.association,
            full_name="Existing Member",
            phone_number="08098765432",
            nin_number="10987654321",
            date_of_birth="2001-01-01",
            institution="Malam Sidi College",
            course="Biology",
            category=Member.Category.UNDERGRADUATE,
            approval_status=Member.ApprovalStatus.APPROVED,
            passport_photo=make_image("existing.png"),
        )
        member.refresh_from_db()
        self.assertTrue(member.membership_id)

        self.set_open(False)
        response = self.client.post(
            reverse("members:portal_login"),
            {
                "method": BY_MEMBERSHIP_ID,
                "membership_id": member.membership_id,
                "phone_number": "08098765432",
            },
        )
        self.assertRedirects(response, reverse("members:portal_dashboard"))
        self.assertEqual(self.client.get(reverse("members:portal_dashboard")).status_code, 200)

    def test_success_page_of_an_earlier_application_still_works_while_closed(self):
        response = self.client.post(reverse("members:register"), _registration_post_data())
        success_url = response["Location"]

        self.set_open(False)
        self.assertEqual(self.client.get(success_url).status_code, 200)


@override_settings(DEFAULT_ASSOCIATION_SLUG="msa")
class AdminRegistrationControlTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("setup_roles", verbosity=0)
        cls.association = Association.objects.create(
            name="Malam Sidi Students Association", short_name="MSA", slug="msa"
        )
        cls.changelist_url = reverse("admin:core_sitesettings_changelist")

    def setUp(self):
        self.site_settings = SiteSettings.objects.create(association=self.association)
        self.change_url = reverse("admin:core_sitesettings_change", args=[self.site_settings.pk])

    # -- helpers ---------------------------------------------------------
    def is_open(self):
        return SiteSettings.objects.get(pk=self.site_settings.pk).registration_open

    def user_in_group(self, username, group_name):
        user = User.objects.create_user(username=username, password="x", is_staff=True)
        user.groups.add(Group.objects.get(name=group_name))
        return user

    def user_with_permissions(self, username, *codenames):
        user = User.objects.create_user(username=username, password="x", is_staff=True)
        user.user_permissions.set(Permission.objects.filter(codename__in=codenames))
        return user

    def run_action(self, action):
        return self.client.post(
            self.changelist_url,
            {"action": action, "_selected_action": [self.site_settings.pk]},
        )

    def form_data(self, **overrides):
        data = {"association": self.association.pk, "_save": "Save"}
        data.update(overrides)
        return data

    # -- who is allowed --------------------------------------------------
    def test_roles_receive_the_permission_from_setup_roles(self):
        def has(role):
            return Group.objects.get(name=role).permissions.filter(codename="manage_registration_status").exists()

        self.assertTrue(has(ROLE_SUPER_ADMIN))
        self.assertTrue(has(ROLE_REGISTRATION_ADMIN))
        self.assertFalse(has(ROLE_ELECTION_ADMIN))
        self.assertFalse(has(ROLE_ANALYTICS_ADMIN))

    # -- opening / closing / reopening via the list actions ---------------
    def test_superuser_can_close_then_reopen_with_the_actions(self):
        superuser = User.objects.create_superuser(username="boss", password="x")
        self.client.force_login(superuser)

        response = self.run_action("close_registration")
        self.assertEqual(response.status_code, 302)
        self.assertFalse(self.is_open())

        self.run_action("open_registration")
        self.assertTrue(self.is_open())

    def test_registration_admin_can_close_and_reopen_with_the_actions(self):
        self.client.force_login(self.user_in_group("reg", ROLE_REGISTRATION_ADMIN))

        self.run_action("close_registration")
        self.assertFalse(self.is_open())
        self.run_action("open_registration")
        self.assertTrue(self.is_open())

    def test_changelist_shows_current_status_and_both_actions(self):
        self.client.force_login(self.user_in_group("reg", ROLE_REGISTRATION_ADMIN))

        response = self.client.get(self.changelist_url)
        self.assertContains(response, "Open membership registration")
        self.assertContains(response, "Close membership registration")
        self.assertContains(response, "sams-badge--success")

        SiteSettings.objects.filter(pk=self.site_settings.pk).update(registration_open=False)
        response = self.client.get(self.changelist_url)
        self.assertContains(response, "sams-badge--error")

    # -- opening / closing / reopening via the settings form ---------------
    def test_change_form_has_a_clear_registration_section_for_authorised_admin(self):
        self.client.force_login(User.objects.create_superuser(username="boss", password="x"))

        response = self.client.get(self.change_url)
        self.assertContains(response, "Membership registration")
        self.assertContains(response, 'name="registration_open"')

    def test_superuser_can_close_and_reopen_with_the_settings_form(self):
        self.client.force_login(User.objects.create_superuser(username="boss", password="x"))

        # An unticked checkbox is simply absent from the posted data.
        self.assertEqual(self.client.post(self.change_url, self.form_data()).status_code, 302)
        self.assertFalse(self.is_open())

        self.client.post(self.change_url, self.form_data(registration_open="on"))
        self.assertTrue(self.is_open())

    # -- who is NOT allowed -------------------------------------------------
    def test_anonymous_user_cannot_change_registration_status(self):
        response = self.run_action("close_registration")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/admin/login/", response["Location"])
        self.assertTrue(self.is_open())

        self.client.post(self.change_url, self.form_data())
        self.assertTrue(self.is_open())

    def test_roles_without_access_cannot_change_registration_status(self):
        for role in (ROLE_ELECTION_ADMIN, ROLE_ANALYTICS_ADMIN):
            with self.subTest(role=role):
                self.client.force_login(self.user_in_group(f"user-{role[:3]}", role))
                response = self.run_action("close_registration")
                self.assertEqual(response.status_code, 403)
                self.assertTrue(self.is_open())
                self.assertEqual(self.client.post(self.change_url, self.form_data()).status_code, 403)
                self.assertTrue(self.is_open())

    def test_view_only_staff_cannot_run_the_actions(self):
        self.client.force_login(self.user_with_permissions("viewer", "view_sitesettings"))

        self.assertEqual(self.client.get(self.changelist_url).status_code, 200)
        self.assertNotContains(self.client.get(self.changelist_url), "Close membership registration")

        self.run_action("close_registration")
        self.assertTrue(self.is_open())

    def test_staff_who_may_edit_site_settings_but_lack_the_permission_cannot_change_status(self):
        SiteSettings.objects.filter(pk=self.site_settings.pk).update(registration_open=False)
        self.client.force_login(
            self.user_with_permissions("editor", "view_sitesettings", "change_sitesettings")
        )

        # The switch is shown read-only, not as an editable checkbox...
        response = self.client.get(self.change_url)
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'name="registration_open"')

        # ...and a forged submission that includes it is ignored.
        response = self.client.post(self.change_url, self.form_data(registration_open="on", motto="Hello"))
        self.assertEqual(response.status_code, 302)
        self.assertFalse(self.is_open())
        self.assertEqual(SiteSettings.objects.get(pk=self.site_settings.pk).motto, "Hello")

        # The actions are not offered or honoured either.
        self.run_action("open_registration")
        self.assertFalse(self.is_open())

    # -- the switch changes only what it should -------------------------------
    def test_closing_via_admin_blocks_public_applications_and_reopening_restores_them(self):
        self.client.force_login(User.objects.create_superuser(username="boss", password="x"))

        self.run_action("close_registration")
        public = Client()
        response = public.get(reverse("members:register"))
        self.assertContains(response, "Registration is currently closed")

        self.run_action("open_registration")
        response = public.get(reverse("members:register"))
        self.assertContains(response, 'name="full_name"')

    def test_closing_registration_does_not_block_other_admin_work(self):
        superuser = User.objects.create_superuser(username="boss", password="x")
        self.client.force_login(superuser)
        self.run_action("close_registration")

        for url_name in ("admin:members_member_changelist", "admin:elections_election_changelist"):
            with self.subTest(url_name=url_name):
                self.assertEqual(self.client.get(reverse(url_name)).status_code, 200)
