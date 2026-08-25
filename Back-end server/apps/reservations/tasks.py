from celery import shared_task
from django.db import transaction
from django.utils import timezone

from .models import Reservation
from book_copies.models import BookCopy  # adjust import path if different


@shared_task(name='reservations.expire_reservations')
def expire_reservations():
    """
    Celery task: expire_reservations

    Finds every reservation that is still marked 'reserved' but whose
    `expires_at` has passed, and:
        1. Flips its status to 'expired'
        2. Frees up the associated book copy back to 'available'
           so it can be reserved or borrowed by someone else.

    `@shared_task` (rather than `@app.task`) is used so this task
    doesn't need to import your specific Celery app instance directly —
    it attaches itself to whichever app is configured when Django
    starts. This is the recommended pattern for tasks defined inside
    reusable apps.

    `name=` sets an explicit, stable task name. Without it, Celery
    infers the name from the module path (e.g.
    'reservations.tasks.expire_reservations'), which would silently
    change if you ever move/rename this file — an explicit name avoids
    that breaking your Celery Beat schedule.

    Returns a short summary string, which Celery stores as the task's
    result (visible in Flower/monitoring tools, or if you chain tasks).
    """
    now = timezone.now()

    expired_reservations = Reservation.objects.select_related('book_copy').filter(
        status=Reservation.STATUS_RESERVED,
        expires_at__lt=now,
    )

    if not expired_reservations.exists():
        return 'No reservations to expire.'

    expired_count = 0

    for reservation in expired_reservations:
        # Each reservation's two related writes (Reservation + BookCopy)
        # are wrapped in their own transaction, so one reservation's
        # failure doesn't roll back or block the others.
        with transaction.atomic():
            reservation.status = Reservation.STATUS_EXPIRED
            reservation.save(update_fields=['status'])

            book_copy = reservation.book_copy
            # Defensive check: only free the copy if it's still
            # 'reserved' — something else may have already changed its
            # status (e.g. marked 'lost' by a librarian in the meantime).
            if book_copy.status == BookCopy.STATUS_RESERVED:
                book_copy.status = BookCopy.STATUS_AVAILABLE
                book_copy.save(update_fields=['status'])

        expired_count += 1

    return f'Successfully expired {expired_count} reservation(s).'