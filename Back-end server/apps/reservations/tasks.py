from celery import shared_task
from django.db import transaction
from django.utils import timezone

from .models import Reservation
from .services import assign_or_release_book_copy
from .notifications import notify_reservation_expired


@shared_task(name='reservations.expire_reservations')
def expire_reservations():
    """
    Finds every reservation still in 'reserved' status (a copy WAS
    assigned, member notified) whose expires_at has passed, and
    expires it.

    CHANGED from the original version: the freed copy is now handed to
    the NEXT waiting reservation for the same book, if one exists —
    via assign_or_release_book_copy(), the SAME function the return
    flow uses. Without this, an expired hold would just sit at
    'available' indefinitely even if other members are waiting, since
    this copy isn't "out on loan" — nothing would trigger fulfillment
    for it again until some unrelated future return.
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
        with transaction.atomic():
            book_copy = reservation.book_copy

            reservation.status = Reservation.STATUS_EXPIRED
            reservation.save(update_fields=['status'])

            # Defensive: only touch the copy if it's still actually
            # sitting 'reserved' — a librarian could have manually
            # changed it (e.g. marked lost/damaged) in the meantime.
            if book_copy is not None and book_copy.status == book_copy.STATUS_RESERVED:
                assign_or_release_book_copy(book_copy)

            notify_reservation_expired(reservation.id)

        expired_count += 1

    return f'Successfully expired {expired_count} reservation(s).'