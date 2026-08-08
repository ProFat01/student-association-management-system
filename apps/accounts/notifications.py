"""
apps.accounts.notifications — SAMS v1.2 Communication Center.

v1.2 Feature 8 asks for the architecture to be built "around a Notification
Service" so that email is just one delivery provider today, and SMS can
become a second provider later "without redesigning anything." This module
is that seam:

  - NotificationProvider is the interface every delivery method implements.
  - EmailProvider is the only *enabled* one right now, and reuses Django's
    existing email infrastructure exactly as apps.members.email_service
    already does for the v1.1 approval email (settings.DEFAULT_FROM_EMAIL,
    settings.EMAIL_BACKEND — nothing new configured here).
  - SMSProvider / PushProvider exist as disabled placeholders per Feature 8
    ("the UI must already include Email / SMS (Coming Soon) / Push
    Notification (Coming Soon)") — calling either raises before anything
    is sent. A future SMS integration means writing one real provider
    class and enabling it here; no view, form, model, or template in this
    module needs to change for that.

This module never touches apps.members.email_service (the approval email)
or its templates — that is a separate, already-approved v1.1 feature this
task is not allowed to modify. Announcements are a distinct kind of email
with their own subject/body, sent through the same Django email backend.
"""
import logging

from django.conf import settings
from django.core.mail import get_connection, send_mail

from .models import Announcement

logger = logging.getLogger("apps.accounts.notifications")


class NotificationProvider:
    key = None
    label = None
    enabled = False

    def send(self, member, subject, message, connection=None):
        raise NotImplementedError


class EmailProvider(NotificationProvider):
    key = "email"
    label = "Email"
    enabled = True

    def send(self, member, subject, message, connection=None):
        """
        Returns (True, None) on a dispatched email, (False, reason) on
        anything else — never raises, matching email_service.py's "SMTP
        failure must not blow up the caller" rule, extended here to cover
        an announcement being sent to hundreds/thousands of members in one
        request: one bad address must not abort the rest of the batch.
        """
        if not member.email:
            return False, "no email on file"
        try:
            send_mail(
                subject=subject,
                message=message,
                from_email=getattr(settings, "DEFAULT_FROM_EMAIL", "noreply@example.com"),
                recipient_list=[member.email],
                fail_silently=False,
                connection=connection,
            )
            return True, None
        except Exception as exc:  # noqa: BLE001 — deliberately broad, see email_service.py's identical reasoning
            logger.exception("Failed to send announcement email to member %s", member.pk)
            return False, str(exc)


class SMSProvider(NotificationProvider):
    key = "sms"
    label = "SMS (Coming Soon)"
    enabled = False

    def send(self, member, subject, message, connection=None):
        raise NotImplementedError("SMS delivery is not implemented yet.")


class PushProvider(NotificationProvider):
    key = "push"
    label = "Push Notification (Coming Soon)"
    enabled = False

    def send(self, member, subject, message, connection=None):
        raise NotImplementedError("Push notification delivery is not implemented yet.")


PROVIDERS = {
    EmailProvider.key: EmailProvider(),
    SMSProvider.key: SMSProvider(),
    PushProvider.key: PushProvider(),
}


def send_announcement(announcement, recipients):
    """
    Sends `announcement` to every Member in `recipients` (an iterable —
    typically the queryset apps.members.services.resolve_announcement_recipients
    returned) via the provider named by announcement.delivery_method, then
    updates and saves the announcement's own sent/failed counts and status.

    Feature 9 (performance): opens ONE SMTP connection for the whole batch
    (get_connection()) instead of one per member — the single most
    impactful thing to do here without Celery/Redis (both explicitly out
    of scope per the brief). This is still a synchronous, in-request send;
    for very large recipient sets on PythonAnywhere Free, see
    PRODUCTION_DEPLOYMENT_2.md "Communication Center: synchronous
    bulk-send limit".
    """
    provider = PROVIDERS.get(announcement.delivery_method)

    if provider is None or not provider.enabled:
        # Feature 8: SMS/Push are UI placeholders only. Defence in depth —
        # the form already rejects anything but "email" (see
        # AnnouncementComposeForm.clean_delivery_method) — this is the
        # second, model-adjacent check in case this function is ever
        # called from somewhere that skipped form validation.
        announcement.status = Announcement.Status.FAILED
        announcement.save(update_fields=["status"])
        return announcement

    sent = 0
    failed = 0
    connection = get_connection() if provider.key == "email" else None
    try:
        if connection is not None:
            connection.open()
        for member in recipients:
            ok, _reason = provider.send(member, announcement.subject, announcement.message, connection=connection)
            if ok:
                sent += 1
            else:
                failed += 1
    finally:
        if connection is not None:
            connection.close()

    announcement.sent_count = sent
    announcement.failed_count = failed
    if sent and not failed:
        announcement.status = Announcement.Status.SENT
    elif sent and failed:
        announcement.status = Announcement.Status.PARTIAL
    else:
        announcement.status = Announcement.Status.FAILED
    announcement.save(update_fields=["sent_count", "failed_count", "status"])
    return announcement
