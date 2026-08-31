import uuid
from django.db import models


class Reservation(models.Model):
    """
    Maps to the `reservations` table in PostgreSQL.

    A Reservation is BOOK-level, not copy-level: it represents "this
    member wants a copy of this book from this library", and only gets
    tied to one specific physical book_copy once one is actually
    assigned to it (status transitions from 'waiting' to 'reserved').
    This is a deliberate redesign from the original copy-level model —
    see the project's design notes for the full reasoning.
    """

    # --- Status choices ---------------------------------------------
    # 'waiting'     -> created, no copy assigned yet ("please let me
    #                  know when a copy of this book is free")
    # 'reserved'    -> a copy HAS been assigned and the member notified
    #                  ("this specific copy is being held for you")
    # 'checked_out' -> member came in and collected the held copy
    # 'cancelled'   -> member/staff cancelled while waiting or reserved
    # 'expired'     -> hold period passed without pickup (only ever
    #                  applies to a 'reserved' reservation)
    STATUS_WAITING = 'waiting'
    STATUS_RESERVED = 'reserved'
    STATUS_CHECKED_OUT = 'checked_out'
    STATUS_CANCELLED = 'cancelled'
    STATUS_EXPIRED = 'expired'

    STATUS_CHOICES = [
        (STATUS_WAITING, 'Waiting'),
        (STATUS_RESERVED, 'Reserved'),
        (STATUS_CHECKED_OUT, 'Checked Out'),
        (STATUS_CANCELLED, 'Cancelled'),
        (STATUS_EXPIRED, 'Expired'),
    ]

    # How many days a member has to collect a copy once it's assigned
    # to them (waiting -> reserved). The clock starts at assignment,
    # not at original reservation creation.
    HOLD_PERIOD_DAYS = 3

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False, db_column='id')

    # NEW — which book this reservation is for. Book-level, not
    # copy-level: this is what lets the reservation exist BEFORE any
    # specific copy is free.
    book = models.ForeignKey(
        'books.Book',
        on_delete=models.CASCADE,
        db_column='book_id',
        related_name='reservations',
    )

    # NEW — which library's stock this reservation watches. Required
    # because the same book title can exist in multiple libraries'
    # catalogs (multi-tenant system) — without this, "a copy of this
    # book became available" would be ambiguous about WHICH library's
    # copy it means.
    library = models.ForeignKey(
        'libraries.Library',
        on_delete=models.CASCADE,
        db_column='library_id',
        related_name='reservations',
    )

    # CHANGED — nullable now (was NOT NULL). NULL while status is
    # 'waiting'; populated only when a matching copy is actually
    # assigned. on_delete changed from CASCADE to SET_NULL: if the
    # assigned copy is later removed (lost/withdrawn), the reservation
    # should fall back to waiting for a different copy, not vanish.
    book_copy = models.ForeignKey(
        'book_copies.BookCopy',
        on_delete=models.SET_NULL,
        db_column='book_copy_id',
        related_name='reservations',
        null=True,
        blank=True,
    )

    member = models.ForeignKey(
        'members.Member',
        on_delete=models.CASCADE,
        db_column='member_id',
        related_name='reservations',
    )

    status = models.CharField(max_length=30, choices=STATUS_CHOICES, default=STATUS_WAITING)

    # When the reservation was first PLACED (unchanged meaning).
    reserved_at = models.DateTimeField(blank=True, null=True)

    # CHANGED meaning — NULL until a copy is actually assigned. Only
    # set at the waiting->reserved transition, to "now + HOLD_PERIOD_DAYS".
    expires_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        managed = False
        db_table = 'reservations'
        ordering = ['-reserved_at']
        verbose_name = 'Reservation'
        verbose_name_plural = 'Reservations'

        # Documentation only (managed=False) — mirrors the indexes
        # created via the raw SQL below.
        indexes = [
            models.Index(fields=['book_copy'], name='idx_reservation_book_id'),
            models.Index(fields=['member'], name='idx_reservation_member_id'),
            models.Index(fields=['status'], name='idx_reservation_status'),
            # Serves expire_reservations' query: status='reserved' AND expires_at < now.
            models.Index(fields=['status', 'expires_at'], name='idx_reservation_status_exp'),
            # Serves fulfillment's query: oldest 'waiting' reservation
            # for a given (library, book) — equality columns first,
            # sort column (reserved_at) last, matching the query shape.
            models.Index(fields=['library', 'book', 'status', 'reserved_at'], name='idx_reservation_fulfill'),
        ]

    def __str__(self):
        return f"Reservation {self.id} ({self.status})"

    def queue_position(self):
        """
        1-based position of this reservation among all still-'waiting'
        reservations for the same (library, book), ordered by
        reserved_at. Computed on read, not stored — it naturally shifts
        as reservations ahead of it get fulfilled or cancelled. Only
        meaningful while status='waiting'; returns None otherwise.
        """
        if self.status != self.STATUS_WAITING:
            return None
        earlier_count = Reservation.objects.filter(
            library=self.library,
            book=self.book,
            status=self.STATUS_WAITING,
            reserved_at__lt=self.reserved_at,
        ).count()
        return earlier_count + 1