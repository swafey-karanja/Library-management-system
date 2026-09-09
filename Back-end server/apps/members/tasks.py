"""
apps/members/tasks.py

Celery tasks for the two member emails: the welcome/confirm-email
message sent on creation, and the "your account is now active" message
sent after confirmation. This is what actually puts those emails through
the broker -> worker pipeline, the same way apps/notifications/tasks.py
already does for checkout/return/reservation/reminder emails.

WHAT "GOING THROUGH THE PIPELINE" MEANS, CONCRETELY:
Before this file existed, views.py called send_member_welcome_email()
and send_member_active_email() (from emails.py) DIRECTLY, inline, as
part of handling the HTTP request. That means the request-response cycle
for "create a member" or "confirm my email" was stuck waiting on a
network call to Resend's API before Django could send a response back —
slow for one member, and a real problem if MemberImportView is asked to
import a few hundred members from a CSV in one request.

Now, instead of calling the sending function directly, views.py calls
`.delay(...)` on the tasks below. `.delay()` does NOT run the function
itself — it serializes the arguments to JSON, drops that JSON message on
the BROKER (Redis, configured via CELERY_BROKER_URL in settings.py), and
returns almost instantly. A separate `celery -A core worker` process,
running independently of the Django server (the "worker"), is what's
actually watching that queue, picks the message up whenever it's free,
and runs the real function — completely decoupled from the original
HTTP request, which already finished and returned a response long before
the email is actually sent.

WHY TASKS TAKE ONLY IDs/PRIMITIVES, NEVER MODEL INSTANCES:
Because the broker only understands JSON (see CELERY_TASK_SERIALIZER in
settings.py), and a Django model instance isn't JSON-serializable — you
can't put a `Member` object on the queue. Even if you serialized it some
other way, the copy sitting in the queue could go stale (e.g. someone
edits the member's email between `.delay()` being called and the worker
actually getting to it minutes later under heavy load). So every task
below takes only `member_id` (a plain string) and re-fetches the current
row from the database itself, right when it actually runs. This exact
reasoning is spelled out in apps/notifications/tasks.py too — same rule,
applied consistently across the whole project.

RETRIES:
`bind=True` gives the task access to `self`, which is what lets us call
`self.retry(...)`. `max_retries=3, default_retry_delay=60` means: if
sending genuinely fails (Resend is down, network hiccup, etc.), Celery
will automatically try again up to 3 more times, waiting 60 seconds
between attempts, before finally giving up and logging it as failed.
"""

import logging

from celery import shared_task

from .models import Member

logger = logging.getLogger(__name__)


@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def send_member_welcome_email_task(self, member_id, confirmation_url):
    """
    Queued by _send_member_activation() in views.py, immediately after a
    MemberActivationToken row is created — for both a single member
    creation (MemberCreateView) and each row of a CSV import
    (MemberImportView).

    Args:
        member_id:          Member's UUID, as a string (see the module
                             docstring on why this is an ID, not the
                             Member instance itself).
        confirmation_url:    The full frontend link, already built by
                              the caller — this task doesn't need to know
                              anything about tokens, it just sends what
                              it's given.
    """
    # Imported here (not at module level) purely to sidestep any
    # app-loading-order edge cases when Celery imports this module —
    # emails.py itself only needs `settings` and `resend`, so this isn't
    # strictly required here the way it is for the cross-app imports in
    # apps/notifications/tasks.py, but keeping the same shape/habit
    # across every task file makes the codebase more predictable.
    from .emails import send_member_welcome_email

    try:
        member = Member.objects.select_related("library").get(pk=member_id)
    except Member.DoesNotExist:
        # The member could have been deleted between the request that
        # created them and the worker getting around to this task. Not
        # much we can do — log it and move on rather than retrying
        # forever for a row that will never come back.
        logger.warning(
            "send_member_welcome_email_task: member %s no longer exists", member_id
        )
        return

    if not member.email:
        logger.info(
            "send_member_welcome_email_task: member %s has no email on file, skipping",
            member_id,
        )
        return

    try:
        send_member_welcome_email(
            member_name=member.name,
            library_name=member.library.name,
            to_email=member.email,
            confirmation_url=confirmation_url,
        )
    except Exception as exc:
        logger.exception(
            "send_member_welcome_email_task failed for member %s", member_id
        )
        # self.retry() re-raises internally in a way Celery understands,
        # which is why this whole call is wrapped in `raise` — without
        # it, the task would look like it finished normally even though
        # the retry was scheduled.
        raise self.retry(exc=exc)


@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def send_member_active_email_task(self, member_id):
    """
    Queued by MemberActivationView in views.py, immediately after a
    member's status is flipped from "inactive" to "active".

    Args:
        member_id: Member's UUID, as a string.
    """
    from .emails import send_member_active_email

    try:
        member = Member.objects.select_related("library").get(pk=member_id)
    except Member.DoesNotExist:
        logger.warning(
            "send_member_active_email_task: member %s no longer exists", member_id
        )
        return

    if not member.email:
        logger.info(
            "send_member_active_email_task: member %s has no email on file, skipping",
            member_id,
        )
        return

    try:
        send_member_active_email(
            member_name=member.name,
            library_name=member.library.name,
            to_email=member.email,
        )
    except Exception as exc:
        logger.exception(
            "send_member_active_email_task failed for member %s", member_id
        )
        raise self.retry(exc=exc)