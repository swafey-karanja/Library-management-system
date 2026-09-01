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
from django.db import transaction
from .notifications import notify_reservation_created, notify_reservation_ready

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

def claim_available_copy(library, book):
    """
    Locks and returns one currently-available book_copy of `book` at
    `library`, if one exists. Used at RESERVATION CREATION time to
    check whether a copy is already sitting on the shelf, so a member
    doesn't get stuck 'waiting' unnecessarily when something's already
    free. Returns None if nothing's available.

    Must be called inside an existing transaction.atomic() block —
    select_for_update() requires one. Locking here (not just reading)
    matters: it stops two reservation requests for the SAME last
    available copy racing each other and both trying to claim it.
    """
    from apps.book_copies.models import BookCopy
    return (
        BookCopy.objects
        .select_for_update()
        .filter(library=library, book=book, status=BookCopy.STATUS_AVAILABLE)
        .order_by('id')
        .first()
    )

def create_reservation(library, book, member):
    """
    The ONLY place a Reservation should be created from — guarantees
    the right status, side effects, and notification all happen
    together, atomically:

      - If a copy is available right now, the reservation is created
        ALREADY 'reserved' against that copy (skips 'waiting'
        entirely), the copy is claimed, expires_at is set, and the
        "ready for pickup" email fires — not the "created" email,
        since sending both back-to-back would be redundant/confusing.
      - Otherwise, the reservation is created 'waiting', with the
        "reservation created / awaiting availability" email.
    """
    with transaction.atomic():
        available_copy = claim_available_copy(library, book)

        if available_copy is not None:
            reservation = Reservation.objects.create(
                book=book,
                library=library,
                member=member,
                status=Reservation.STATUS_RESERVED,
                reserved_at=timezone.now(),
                book_copy=available_copy,
                expires_at=timezone.now() + timedelta(days=Reservation.HOLD_PERIOD_DAYS),
            )
            available_copy.status = available_copy.STATUS_RESERVED
            available_copy.save(update_fields=['status'])
            notify_reservation_ready(reservation.id)
        else:
            reservation = Reservation.objects.create(
                book=book,
                library=library,
                member=member,
                status=Reservation.STATUS_WAITING,
                reserved_at=timezone.now(),
            )
            notify_reservation_created(reservation.id)

    return reservation

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