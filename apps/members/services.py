"""
Shared services for apps.members.

`find_member_by_credentials` is the one place that knows how to look a
Member up by (Membership ID + Phone) or (NIN + Phone) without a Django
User account — there is no self-service login on Member itself (see the
`user` field's docstring on Member in models.py), so any feature that
needs to verify "is this really you" from those two credential pairs
calls this instead of re-writing the query.

Two callers use it:
  - apps.elections.forms.VotingLoginForm.authenticate() (pre-existing;
    refactored to call this instead of duplicating the lookup)
  - apps.members.forms.PortalLoginForm.authenticate() (Stage 8: Member
    Self-Service Portal)

This function deliberately stops at "find the member" — it does not
check approval_status or voting_status. Those are caller-specific
eligibility rules (voting requires voting_status; the member portal
just requires approval_status == APPROVED), so they stay out of the
shared lookup and are decided by each form/view instead.
"""
from .models import Member

BY_MEMBERSHIP_ID = "membership_id"
BY_NIN = "nin"

CREDENTIAL_METHOD_CHOICES = [
    (BY_MEMBERSHIP_ID, "Membership ID + Phone Number"),
    (BY_NIN, "NIN + Phone Number"),
]


def find_member_by_credentials(method, identifier, phone_number):
    """
    Returns the matching Member, or None. `identifier` is a
    membership_id when method == BY_MEMBERSHIP_ID, or an nin_number when
    method == BY_NIN.
    """
    identifier = (identifier or "").strip()
    phone_number = (phone_number or "").strip()
    if not identifier or not phone_number:
        return None

    if method == BY_MEMBERSHIP_ID:
        return Member.objects.filter(membership_id=identifier, phone_number=phone_number).first()
    if method == BY_NIN:
        return Member.objects.filter(nin_number=identifier, phone_number=phone_number).first()
    return None
