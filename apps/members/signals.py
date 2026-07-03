"""
Keeps Member.approval_status / membership_id / voting_status in sync with
the RegistrationApplication that was actually reviewed, so admins always
review through RegistrationApplication (the auditable workflow object —
who reviewed it, when, why rejected) and never have to remember to also
flip flags on the related Member by hand.

Also deletes the application's receipt file the moment a review decision
is recorded (approved OR rejected) — the receipt is only ever needed
*during* review; once a decision exists, leaving the payment-proof image
on disk indefinitely is pure liability with no remaining purpose. This
fires for every transition out of Pending, so there's exactly one place
that does it rather than relying on every call site (admin action, future
API) to remember.
"""
from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver
from django.utils import timezone

from .models import Member, RegistrationApplication
from .utils import generate_membership_id


@receiver(pre_save, sender=RegistrationApplication)
def _stash_previous_status(sender, instance, **kwargs):
    """Remember the pre-save status so post_save can detect a real transition."""
    if instance.pk:
        previous = (
            RegistrationApplication.objects.filter(pk=instance.pk)
            .values_list("status", flat=True)
            .first()
        )
        instance._previous_status = previous
    else:
        instance._previous_status = None

    # Auto-stamp reviewed_at the moment a decision is first recorded, so
    # admins reviewing through the Django admin don't have to set it by
    # hand and it can never be back-dated by mistake.
    if instance._previous_status == RegistrationApplication.Status.PENDING.value and (
        instance.status != RegistrationApplication.Status.PENDING.value
    ):
        instance.reviewed_at = timezone.now()


@receiver(post_save, sender=RegistrationApplication)
def _sync_member_on_review(sender, instance, created, **kwargs):
    previous = getattr(instance, "_previous_status", None)
    if previous == instance.status:
        return  # no actual transition (e.g. editing rejection_reason text later)

    member = instance.member

    if instance.status == RegistrationApplication.Status.APPROVED:
        member.approval_status = Member.ApprovalStatus.APPROVED
        member.voting_status = True
        if not member.membership_id:
            member.membership_id = generate_membership_id(member.association)
        member.save(update_fields=["approval_status", "voting_status", "membership_id"])
        instance.clear_receipt()  # PART 6: receipt is no longer needed once a decision exists

    elif instance.status == RegistrationApplication.Status.REJECTED:
        member.approval_status = Member.ApprovalStatus.REJECTED
        member.voting_status = False
        member.save(update_fields=["approval_status", "voting_status"])
        instance.clear_receipt()  # same cleanup on rejection — both outcomes are "reviewed"
