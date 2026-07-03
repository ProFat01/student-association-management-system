"""
Custom auth user model.

We don't need extra *fields* yet, but Django strongly recommends starting
every new project with a custom user model because swapping
AUTH_USER_MODEL after the first migration touches every FK to auth.User
in the database. Doing it now costs nothing and buys total freedom later
(e.g. adding 2FA fields, or letting Members log in to a future
self-service portal without a second user table).

Role enforcement itself is NOT done with a field on this model — see
ARCHITECTURE.md "Permission architecture" for the reasoning — it is done
with `django.contrib.auth.models.Group` ("Super Admin", "Registration
Admin", "Election Admin", "Analytics Admin"), seeded by the
`setup_roles` management command in apps/accounts/management/commands/.
"""
from django.contrib.auth.models import AbstractUser
from django.db import models

from apps.core.models import Association

# Group names used throughout the project. Defined once here (rather than
# as magic strings scattered across admin.py files) so renaming a role is
# a one-line change.
ROLE_SUPER_ADMIN = "Super Admin"
ROLE_REGISTRATION_ADMIN = "Registration Admin"
ROLE_ELECTION_ADMIN = "Election Admin"
ROLE_ANALYTICS_ADMIN = "Analytics Admin"

ALL_ROLES = [ROLE_SUPER_ADMIN, ROLE_REGISTRATION_ADMIN, ROLE_ELECTION_ADMIN, ROLE_ANALYTICS_ADMIN]


class User(AbstractUser):
    """
    SAMS staff/admin account. Ordinary registrants do NOT need one of
    these to apply for membership (see apps.members.Member), only people
    who administer the system through Django admin (or a future custom
    dashboard) do.
    """

    association = models.ForeignKey(
        Association,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="staff_users",
        help_text=(
            "Scopes a non-superuser admin to a single association. Leave "
            "blank for Super Admins, who are expected to be Django "
            "superusers and therefore see every association."
        ),
    )
    phone_number = models.CharField(max_length=20, blank=True)

    class Meta:
        verbose_name = "Staff User"
        verbose_name_plural = "Staff Users"

    def __str__(self):
        return self.get_full_name() or self.username

    @property
    def role_names(self):
        """Cheap helper for templates/admin checks: ['Registration Admin', ...]."""
        return list(self.groups.values_list("name", flat=True))

    def has_role(self, role_name):
        return self.is_superuser or self.groups.filter(name=role_name).exists()
