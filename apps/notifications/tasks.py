"""
apps/notifications/tasks.py

Celery tasks for member email notifications tied to borrow transactions
(checkout, return, and librarian corrections).

WHY TASKS TAKE ONLY IDS/PRIMITIVES, NEVER MODEL INSTANCES:
Celery serializes task arguments (JSON by default) to put them on the
broker. A Django model instance either can't survive that trip, or -
worse - "survives" as a stale snapshot of whatever it looked like at
the moment .delay() was called, which may not match the DB by the time
a worker actually picks the task up. So every task below accepts only
UUIDs (as strings) and plain dicts, and re-fetches whatever it needs
from the DB itself when it runs.

WHY ONE TASK CALL COVERS A WHOLE CHECKOUT/RETURN BATCH:
BorrowBatchCheckoutView and BorrowBatchReturnView always operate on ONE
member at a time (see their serializers - `member` is a single field,
`book_copies`/`transactions` is the list). So "all transactions from
one request" and "all transactions for one member, in one email" are
the same grouping - no need to split by member inside these tasks.

Register this app with your Celery config as normal (CELERY_APP.autodiscover_tasks()
picking up apps.notifications, or an explicit include) - not shown here
since that's already set up in your project.
"""
import logging
from decimal import Decimal

from celery import shared_task
from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string

logger = logging.getLogger(__name__)


def _send_email(subject, template_base, context, to_email):
    """
    Renders templates/notifications/{template_base}.txt and .html and
    sends a multipart email (HTML with a plain-text fallback for
    clients/spam filters that prefer it).
    """
    text_body = render_to_string(f"notifications/{template_base}.txt", context)
    html_body = render_to_string(f"notifications/{template_base}.html", context)

    message = EmailMultiAlternatives(
        subject=subject,
        body=text_body,
        from_email=settings.RESEND_FROM_EMAIL,
        to=[to_email],
    )
    message.attach_alternative(html_body, "text/html")
    message.send(fail_silently=False)


@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def send_checkout_email(self, transaction_ids):
    """
    transaction_ids: list of BorrowTransaction UUIDs (strings). One
    item for a single checkout, several for a batch checkout - all
    assumed to belong to the same member (see module docstring).
    """
    from apps.borrow_transactions.models import BorrowTransaction

    transactions = list(
        BorrowTransaction.objects.select_related("member", "book_copy", "book_copy__book")
        .filter(id__in=transaction_ids)
    )
    if not transactions:
        logger.warning("send_checkout_email: no transactions found for ids %s", transaction_ids)
        return

    member = transactions[0].member
    if not member.email:
        logger.info("send_checkout_email: member %s has no email on file, skipping", member.member_id)
        return

    books = [
        {
            "title": txn.book_copy.book.title,
            "barcode": txn.book_copy.barcode,
            "borrowed_at": txn.borrowed_at,
            "due_date": txn.due_date,
        }
        for txn in transactions
    ]

    subject = "You've checked out a book" if len(books) == 1 else f"You've checked out {len(books)} books"

    try:
        _send_email(
            subject=subject,
            template_base="checkout",
            context={"member": member, "books": books},
            to_email=member.email,
        )
    except Exception as exc:
        logger.exception("send_checkout_email failed for member %s", member.member_id)
        raise self.retry(exc=exc)


@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def send_return_email(self, transaction_ids):
    """Same shape as send_checkout_email - see its docstring."""
    from apps.borrow_transactions.models import BorrowTransaction

    transactions = list(
        BorrowTransaction.objects.select_related("member", "book_copy", "book_copy__book")
        .filter(id__in=transaction_ids)
    )
    if not transactions:
        logger.warning("send_return_email: no transactions found for ids %s", transaction_ids)
        return

    member = transactions[0].member
    if not member.email:
        logger.info("send_return_email: member %s has no email on file, skipping", member.member_id)
        return

    books = [
        {
            "title": txn.book_copy.book.title,
            "barcode": txn.book_copy.barcode,
            "returned_at": txn.returned_at,
            "fine_amount": txn.fine_amount,
        }
        for txn in transactions
    ]
    total_fines = sum((book["fine_amount"] for book in books), Decimal("0.00"))

    subject = "You've returned a book" if len(books) == 1 else f"You've returned {len(books)} books"

    try:
        _send_email(
            subject=subject,
            template_base="return",
            context={"member": member, "books": books, "total_fines": total_fines},
            to_email=member.email,
        )
    except Exception as exc:
        logger.exception("send_return_email failed for member %s", member.member_id)
        raise self.retry(exc=exc)


@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def send_update_email(self, transaction_id, changes):
    """
    transaction_id: a single BorrowTransaction UUID (string) - one task
    call per transaction, even during a bulk-update, since a single
    bulk-update request can touch several DIFFERENT members' loans at
    once (unlike checkout/return, which are always one member per
    request) - there's no shared "batch" grouping to exploit here.

    changes: dict of {field_name: {"old": ..., "new": ...}}, already
    computed and stringified by the caller in
    apps/borrow_transactions/notifications.py - only fields that
    actually changed are included.
    """
    from apps.borrow_transactions.models import BorrowTransaction

    if not changes:
        return

    try:
        txn = BorrowTransaction.objects.select_related(
            "member", "book_copy", "book_copy__book"
        ).get(id=transaction_id)
    except BorrowTransaction.DoesNotExist:
        logger.warning("send_update_email: transaction %s no longer exists", transaction_id)
        return

    member = txn.member
    if not member.email:
        logger.info("send_update_email: member %s has no email on file, skipping", member.member_id)
        return

    # Human-friendly labels for the fields we allow notifying on -
    # kept here (not in the view) since it's purely a presentation
    # concern for this email.
    field_labels = {
        "due_date": "Due date",
        "returned_at": "Returned date",
        "status": "Status",
        "fine_amount": "Fine amount",
    }
    readable_changes = [
        {"label": field_labels.get(field, field), "old": diff["old"], "new": diff["new"]}
        for field, diff in changes.items()
    ]

    try:
        _send_email(
            subject="An update was made to your loan",
            template_base="transaction_update",
            context={
                "member": member,
                "book_title": txn.book_copy.book.title,
                "barcode": txn.book_copy.barcode,
                "changes": readable_changes,
            },
            to_email=member.email,
        )
    except Exception as exc:
        logger.exception("send_update_email failed for transaction %s", transaction_id)
        raise self.retry(exc=exc)