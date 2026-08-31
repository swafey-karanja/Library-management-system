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
from collections import defaultdict
from datetime import timedelta

from django.utils import timezone

logger = logging.getLogger(__name__)

# How far ahead of due_date to send the "due soon" reminder.
DUE_SOON_LEAD_HOURS = 24

# The due-soon scan runs every DUE_SOON_SCAN_INTERVAL_HOURS (see the
# Celery Beat schedule in settings.py — keep these two numbers in
# sync if you ever change the schedule). The candidate window below
# is made TWICE as wide as a single scan interval on purpose: it
# spans two intervals' worth of time around the 24h mark, so that if
# one scheduled run is ever missed (worker restart, broker hiccup,
# etc), the very next run still catches every transaction that
# should have been reminded. NotificationLog's unique constraint
# guarantees this redundancy never causes a duplicate send — only
# acts as a backstop against a missed one.
DUE_SOON_SCAN_INTERVAL_HOURS = 6

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

@shared_task
def send_due_soon_reminders():
    """
    Runs every 6 hours (see CELERY_BEAT_SCHEDULE). Finds active loans
    due in roughly the next 24-30 hours that haven't already been
    reminded about, groups them by member, and sends ONE email per
    member listing everything due soon.
    """
    from apps.borrow_transactions.models import BorrowTransaction
    from .models import NotificationLog

    now = timezone.now()
    window_start = now + timedelta(hours=DUE_SOON_LEAD_HOURS - DUE_SOON_SCAN_INTERVAL_HOURS)
    window_end = now + timedelta(hours=DUE_SOON_LEAD_HOURS + DUE_SOON_SCAN_INTERVAL_HOURS)

    candidates = list(
        BorrowTransaction.objects.select_related("member", "book_copy", "book_copy__book")
        .filter(returned_at__isnull=True, due_date__gte=window_start, due_date__lt=window_end)
    )
    if not candidates:
        return

    # One query to find everything already sent, instead of one query
    # per candidate (N+1) — see the earlier note on this exact risk.
    already_sent = set(
        NotificationLog.objects.filter(
            notification_type=NotificationLog.TYPE_DUE_SOON,
            borrow_transaction_id__in=[txn.id for txn in candidates],
        ).values_list("borrow_transaction_id", "sent_for_date")
    )
    pending = [
        txn for txn in candidates
        if (txn.id, txn.due_date.date()) not in already_sent
    ]
    if not pending:
        return

    by_member = defaultdict(list)
    for txn in pending:
        by_member[txn.member_id].append(txn)

    for member_id, txns in by_member.items():
        member = txns[0].member
        if not member.email:
            logger.info("send_due_soon_reminders: member %s has no email on file, skipping", member_id)
            continue

        books = [
            {
                "title": txn.book_copy.book.title,
                "barcode": txn.book_copy.barcode,
                "due_date": txn.due_date,
            }
            for txn in txns
        ]
        subject = (
            "A book is due soon" if len(books) == 1
            else f"{len(books)} books are due soon"
        )

        try:
            _send_email(
                subject=subject,
                template_base="due_soon",
                context={"member": member, "books": books},
                to_email=member.email,
            )
        except Exception:
            # Don't let one member's send failure stop the rest of the
            # batch, and DON'T log these as sent — leaving them
            # unlogged means the NEXT scan run simply picks these
            # transactions back up and retries them naturally, no
            # separate retry mechanism needed.
            logger.exception("send_due_soon_reminders failed for member %s", member_id)
            continue

        NotificationLog.objects.bulk_create(
            [
                NotificationLog(
                    borrow_transaction_id=txn.id,
                    notification_type=NotificationLog.TYPE_DUE_SOON,
                    sent_for_date=txn.due_date.date(),
                )
                for txn in txns
            ],
            ignore_conflicts=True,  # belt-and-suspenders against a race with another run
        )


@shared_task
def send_overdue_reminders():
    """
    Runs once daily (see CELERY_BEAT_SCHEDULE). Finds every active
    loan that's currently overdue, recalculates and PERSISTS its
    current fine_amount (previously this only happened at return —
    see the fine-calculation review earlier in this project), groups
    by member, and sends one "these books are overdue" email per
    member.
    """
    from apps.borrow_transactions.models import BorrowTransaction
    from .models import NotificationLog

    now = timezone.now()
    today = now.date()

    overdue_transactions = list(
        BorrowTransaction.objects.select_related("member", "book_copy", "book_copy__book")
        .filter(returned_at__isnull=True, due_date__lt=now)
    )
    if not overdue_transactions:
        return

    # Recalculate and persist the live fine for every overdue loan
    # while we're already scanning them — keeps fine_amount realistic
    # in the API/admin for loans that are still out, not just at
    # return time (this was previously stale at 0.00 until return —
    # see the earlier fine-calculation confirmation).
    for txn in overdue_transactions:
        txn.fine_amount = txn.calculate_fine()
    BorrowTransaction.objects.bulk_update(overdue_transactions, ["fine_amount"])

    already_sent = set(
        NotificationLog.objects.filter(
            notification_type=NotificationLog.TYPE_OVERDUE,
            sent_for_date=today,
            borrow_transaction_id__in=[txn.id for txn in overdue_transactions],
        ).values_list("borrow_transaction_id", flat=True)
    )
    pending = [txn for txn in overdue_transactions if txn.id not in already_sent]
    if not pending:
        return

    by_member = defaultdict(list)
    for txn in pending:
        by_member[txn.member_id].append(txn)

    for member_id, txns in by_member.items():
        member = txns[0].member
        if not member.email:
            logger.info("send_overdue_reminders: member %s has no email on file, skipping", member_id)
            continue

        books = [
            {
                "title": txn.book_copy.book.title,
                "barcode": txn.book_copy.barcode,
                "due_date": txn.due_date,
                "days_overdue": txn.days_overdue,
                "fine_amount": txn.fine_amount,
            }
            for txn in txns
        ]
        total_fines = sum((book["fine_amount"] for book in books), Decimal("0.00"))
        subject = (
            "A book is overdue" if len(books) == 1
            else f"{len(books)} books are overdue"
        )

        try:
            _send_email(
                subject=subject,
                template_base="overdue",
                context={"member": member, "books": books, "total_fines": total_fines},
                to_email=member.email,
            )
        except Exception:
            logger.exception("send_overdue_reminders failed for member %s", member_id)
            continue

        NotificationLog.objects.bulk_create(
            [
                NotificationLog(
                    borrow_transaction_id=txn.id,
                    notification_type=NotificationLog.TYPE_OVERDUE,
                    sent_for_date=today,
                )
                for txn in txns
            ],
            ignore_conflicts=True,
        )

@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def send_reservation_created_email(self, reservation_id):
    """One reservation per call — fired once, right after creation."""
    from apps.reservations.models import Reservation

    try:
        reservation = Reservation.objects.select_related('member', 'book').get(id=reservation_id)
    except Reservation.DoesNotExist:
        logger.warning("send_reservation_created_email: reservation %s no longer exists", reservation_id)
        return

    member = reservation.member
    if not member.email:
        logger.info("send_reservation_created_email: member %s has no email on file, skipping", member.member_id)
        return

    try:
        _send_email(
            subject="Your reservation has been placed",
            template_base="reservation_created",
            context={"member": member, "book_title": reservation.book.title},
            to_email=member.email,
        )
    except Exception as exc:
        logger.exception("send_reservation_created_email failed for reservation %s", reservation_id)
        raise self.retry(exc=exc)


@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def send_reservation_cancelled_email(self, reservation_id):
    from apps.reservations.models import Reservation

    try:
        reservation = Reservation.objects.select_related('member', 'book').get(id=reservation_id)
    except Reservation.DoesNotExist:
        logger.warning("send_reservation_cancelled_email: reservation %s no longer exists", reservation_id)
        return

    member = reservation.member
    if not member.email:
        logger.info("send_reservation_cancelled_email: member %s has no email on file, skipping", member.member_id)
        return

    try:
        _send_email(
            subject="Your reservation has been cancelled",
            template_base="reservation_cancelled",
            context={"member": member, "book_title": reservation.book.title},
            to_email=member.email,
        )
    except Exception as exc:
        logger.exception("send_reservation_cancelled_email failed for reservation %s", reservation_id)
        raise self.retry(exc=exc)


@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def send_reservation_ready_email(self, reservation_id):
    """Fired once, at the moment a copy is assigned to the reservation."""
    from apps.reservations.models import Reservation

    try:
        reservation = Reservation.objects.select_related('member', 'book').get(id=reservation_id)
    except Reservation.DoesNotExist:
        logger.warning("send_reservation_ready_email: reservation %s no longer exists", reservation_id)
        return

    member = reservation.member
    if not member.email:
        logger.info("send_reservation_ready_email: member %s has no email on file, skipping", member.member_id)
        return

    if not reservation.expires_at:
        logger.warning("send_reservation_ready_email: reservation %s has no expires_at set", reservation_id)
        return

    try:
        _send_email(
            subject="Your reserved book is ready for collection",
            template_base="reservation_ready",
            context={
                "member": member,
                "book_title": reservation.book.title,
                "reservation_expiry_date": reservation.expires_at,
            },
            to_email=member.email,
        )
    except Exception as exc:
        logger.exception("send_reservation_ready_email failed for reservation %s", reservation_id)
        raise self.retry(exc=exc)


@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def send_reservation_expired_email(self, reservation_id):
    from apps.reservations.models import Reservation

    try:
        reservation = Reservation.objects.select_related('member', 'book').get(id=reservation_id)
    except Reservation.DoesNotExist:
        logger.warning("send_reservation_expired_email: reservation %s no longer exists", reservation_id)
        return

    member = reservation.member
    if not member.email:
        logger.info("send_reservation_expired_email: member %s has no email on file, skipping", member.member_id)
        return

    try:
        _send_email(
            subject="Your reservation has expired",
            template_base="reservation_expired",
            context={"member": member, "book_title": reservation.book.title},
            to_email=member.email,
        )
    except Exception as exc:
        logger.exception("send_reservation_expired_email failed for reservation %s", reservation_id)
        raise self.retry(exc=exc)


@shared_task
def send_reservation_reminders():
    """
    Runs once daily (see CELERY_BEAT_SCHEDULE). Finds every reservation
    currently 'reserved' (copy assigned, awaiting pickup) that hasn't
    expired yet, and reminds the member. Deduped via NotificationLog's
    new reservation_id field, same shape/reasoning as
    send_due_soon_reminders/send_overdue_reminders above — a Beat
    misfire/retry within the same day can't double-send.
    """
    from apps.reservations.models import Reservation
    from .models import NotificationLog

    now = timezone.now()
    today = now.date()

    pending_reservations = list(
        Reservation.objects.select_related('member', 'book').filter(
            status=Reservation.STATUS_RESERVED,
            expires_at__gte=now,
        )
    )
    if not pending_reservations:
        return 'No pending reservations to remind.'

    already_sent = set(
        NotificationLog.objects.filter(
            notification_type=NotificationLog.TYPE_RESERVATION_REMINDER,
            sent_for_date=today,
            reservation_id__in=[r.id for r in pending_reservations],
        ).values_list('reservation_id', flat=True)
    )
    pending = [r for r in pending_reservations if r.id not in already_sent]
    if not pending:
        return 'No pending reservations to remind (already sent today).'

    sent_count = 0
    logged_ids = []

    for reservation in pending:
        member = reservation.member
        if not member.email:
            logger.info("send_reservation_reminders: member %s has no email on file, skipping", member.member_id)
            continue

        try:
            _send_email(
                subject="Reminder: your reserved book is waiting for collection",
                template_base="reservation_reminder",
                context={
                    "member": member,
                    "book_title": reservation.book.title,
                    "reservation_expiry_date": reservation.expires_at,
                },
                to_email=member.email,
            )
        except Exception:
            logger.exception("send_reservation_reminders failed for reservation %s", reservation.id)
            continue

        logged_ids.append(reservation.id)
        sent_count += 1

    NotificationLog.objects.bulk_create(
        [
            NotificationLog(
                reservation_id=reservation_id,
                notification_type=NotificationLog.TYPE_RESERVATION_REMINDER,
                sent_for_date=today,
            )
            for reservation_id in logged_ids
        ],
        ignore_conflicts=True,
    )

    return f"Sent {sent_count} reservation reminder email(s)."