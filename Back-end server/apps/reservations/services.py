"""
apps/reservations/services.py

Business logic OTHER apps call into — mainly borrow_transactions, at
the moment a book_copy would otherwise become available. Deliberately
lives HERE, not in borrow_transactions: "does anyone get first dibs on
this copy" is a reservations concern. Centralizing it means every
return path (single, batch, librarian correction) gets IDENTICAL
fulfillment behavior by calling one function, instead of three
hand-maintained copies of similar logic.
"""
from datetime import timedelta

from django.utils import timezone

from .models import Reservation
from .notifications import notify_reservation_ready


def library_carries_book(library, book):
    """
    True if `library` owns at least one book_copy of `book`, regardless
    of that copy's current status. Used to reject a reservation request
    for a title this library has simply never stocked — a reservation
    only makes sense if there's something that could eventually be
    returned.
    """
    from apps.book_copies.models import BookCopy
    return BookCopy.objects.filter(library=library, book=book).exists()


def assign_or_release_book_copy(book_copy):
    """
    Call this whenever a book_copy WOULD otherwise become available —
    a normal (non-damaged) single return, batch return, a librarian
    correction that sets a transaction back to 'returned', or an
    expiring reservation freeing its copy back up. Decides what
    actually happens to the copy:

      - If a member is waiting for this book at this library, the copy
        is handed to them: assigned to the OLDEST waiting reservation
        (FIFO via reserved_at), that reservation moves from 'waiting'
        to 'reserved', its expires_at is set (the pickup-deadline clock
        starts NOW), and a "ready for pickup" email is queued.
      - If nobody is waiting, the copy just becomes available, exactly
        as it did before reservations existed.

    IMPORTANT: this does NOT open its own transaction.atomic() block —
    it assumes the CALLER already has one open (every call site already
    does). This lets the reservation claim and the copy's status change
    commit/roll back together with the rest of the caller's writes.

    NEVER call this for a copy coming back DAMAGED — a damaged copy
    must not be handed to a waiting member. Callers must route the
    damaged branch around this function entirely.
    """
    # select_for_update() locks the oldest matching reservation so two
    # near-simultaneous returns (of two different copies of the same
    # book) can't both claim it. Combined with the caller's existing
    # atomic() block, this mirrors the same "lock, then check, then
    # act" pattern used throughout borrow_transactions (e.g. checkout's
    # select_for_update on the BookCopy row).
    reservation = (
        Reservation.objects
        .select_for_update()
        .select_related('member', 'book')
        .filter(
            library=book_copy.library,
            book=book_copy.book,
            status=Reservation.STATUS_WAITING,
        )
        .order_by('reserved_at')
        .first()
    )

    if reservation is None:
        book_copy.status = book_copy.STATUS_AVAILABLE
        book_copy.save(update_fields=['status'])
        return

    reservation.book_copy = book_copy
    reservation.status = Reservation.STATUS_RESERVED
    reservation.expires_at = timezone.now() + timedelta(days=Reservation.HOLD_PERIOD_DAYS)
    reservation.save(update_fields=['book_copy', 'status', 'expires_at'])

    book_copy.status = book_copy.STATUS_RESERVED
    book_copy.save(update_fields=['status'])

    notify_reservation_ready(reservation.id)


def close_matching_reservation(member, book_copy):
    """
    Call this as part of a normal checkout (BorrowCheckoutView /
    BorrowBatchCheckoutView), inside the same transaction.atomic()
    block as the rest of checkout. Looks for a 'reserved' reservation
    belonging to THIS member for THIS exact copy — by the time a copy
    is 'reserved', it's already assigned to one specific member's hold,
    so matching on book_copy is both correct and cheap.

    If found, closes it to 'checked_out'. If not found, this is just
    an ordinary walk-in checkout — does nothing; checkout proceeds
    normally either way.
    """
    reservation = Reservation.objects.filter(
        book_copy=book_copy,
        member=member,
        status=Reservation.STATUS_RESERVED,
    ).first()

    if reservation is not None:
        reservation.status = Reservation.STATUS_CHECKED_OUT
        reservation.save(update_fields=['status'])