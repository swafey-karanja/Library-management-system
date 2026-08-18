"""
apps/borrow_transactions/notifications.py

Thin glue between borrow_transactions/views.py and the Celery tasks in
apps.notifications.tasks. views.py only ever calls the three functions
below - it never imports Celery or the task module directly. That
keeps two concerns separate:
  - views.py:        WHEN a notification should fire, and what data
                      is available to build it from.
  - this module:      HOW that gets scheduled (on_commit + .delay()),
                      and what "changed" means for update emails.
  - notifications/tasks.py: HOW the email itself gets rendered/sent.

If the notification strategy changes later (e.g. which fields trigger
an update email, or swapping Celery for something else), this is the
one file to touch - views.py doesn't change.
"""
from django.db import transaction

from apps.notifications.tasks import send_checkout_email, send_return_email, send_update_email

# Fields worth emailing a member about when a librarian corrects a
# transaction via BorrowTransactionUpdateView / BorrowTransactionBulkUpdateView.
# Deliberately doesn't include every writable field - e.g. book_copy/member
# reassignment isn't email-worthy the same way a due_date or fine change is,
# and isn't bulk-editable anyway (see BULK_UPDATABLE_FIELDS in serializers.py).
NOTIFIABLE_FIELDS = ("due_date", "returned_at", "status", "fine_amount")


def notify_checkout(transaction_ids):
    """
    transaction_ids: iterable of BorrowTransaction ids (UUID objects or
    strings), all belonging to the SAME member - true for both
    BorrowCheckoutView (one id) and BorrowBatchCheckoutView (several).
    """
    ids = [str(pk) for pk in transaction_ids]
    if ids:
        transaction.on_commit(lambda: send_checkout_email.delay(ids))


def notify_return(transaction_ids):
    """Same contract as notify_checkout - see its docstring."""
    ids = [str(pk) for pk in transaction_ids]
    if ids:
        transaction.on_commit(lambda: send_return_email.delay(ids))


def notify_update(transaction_id, before, after):
    """
    before / after: dicts of {field_name: value} for the fields in
    NOTIFIABLE_FIELDS, captured by the caller immediately before and
    after a save. Only fields whose value actually changed are
    included in the email - e.g. a PATCH that only touches due_date
    won't mention status even though it was "in" the update request.

    Values are converted to strings before being handed to the task,
    since Decimal/datetime/None need to survive a JSON round-trip
    through the Celery broker.
    """
    changes = {
        field: {"old": before.get(field), "new": after.get(field)}
        for field in NOTIFIABLE_FIELDS
        if before.get(field) != after.get(field)
    }
    if not changes:
        return

    serializable_changes = {
        field: {
            "old": str(diff["old"]) if diff["old"] is not None else None,
            "new": str(diff["new"]) if diff["new"] is not None else None,
        }
        for field, diff in changes.items()
    }

    transaction.on_commit(
        lambda: send_update_email.delay(str(transaction_id), serializable_changes)
    )