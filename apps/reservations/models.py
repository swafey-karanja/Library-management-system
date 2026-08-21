import uuid
from django.db import models


class Reservation(models.Model):
    """
    Maps to the existing `reservations` table in PostgreSQL.

    A Reservation represents a member "holding" a specific physical
    copy of a book (book_copy) until they collect it or it expires.
    """

    # --- Status choices -------------------------------------------------
    # Django's `choices` doesn't create a DB-level CHECK constraint on its
    # own — your table already has one in SQL. This just gives us
    # validation on the Python/DRF side and nice labels in the admin/API.
    STATUS_RESERVED = 'reserved'
    STATUS_CHECKED_OUT = 'checked_out'
    STATUS_CANCELLED = 'cancelled'
    STATUS_EXPIRED = 'expired'

    STATUS_CHOICES = [
        (STATUS_RESERVED, 'Reserved'),
        (STATUS_CHECKED_OUT, 'Checked Out'),
        (STATUS_CANCELLED, 'Cancelled'),
        (STATUS_EXPIRED, 'Expired'),
    ]

    # --- Fields -----------------------------------------------------------
    # `primary_key=True` tells Django this is the PK, matching `id UUID PRIMARY KEY`.
    # `default=uuid.uuid4` means Django can generate one client-side if we ever
    # create a Reservation via the ORM. It doesn't override the DB's
    # `gen_random_uuid()` default — that only kicks in if Django sends no value.
    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
        db_column='id',
    )

    # ForeignKey to BookCopy. Adjust 'books.BookCopy' to match whatever
    # app/model name you used for the book_copies table.
    # db_column tells Django the actual column name in Postgres is
    # `book_copy_id`, since Django would otherwise guess `book_copy_id_id`
    # (it appends `_id` automatically to FK field names).
    book_copy = models.ForeignKey(
        'book_copies.BookCopy',
        on_delete=models.CASCADE,   # mirrors "ON DELETE CASCADE" in SQL
        db_column='book_copy_id',
        related_name='reservations',  # lets you do book_copy.reservations.all()
    )

    # ForeignKey to Member. Adjust 'members.Member' to your actual app/model name.
    member = models.ForeignKey(
        'members.Member',
        on_delete=models.CASCADE,
        db_column='member_id',
        related_name='reservations',
    )

    status = models.CharField(
        max_length=30,
        choices=STATUS_CHOICES,
        default=STATUS_RESERVED,
    )

    # These have DB-side defaults (CURRENT_TIMESTAMP), so we allow them
    # to be blank/null from Django's perspective — Postgres fills them in
    # if we don't supply a value on INSERT.
    reserved_at = models.DateTimeField(blank=True, null=True)
    expires_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        managed = False          # Django will NOT create/alter/drop this table
        db_table = 'reservations'  # exact table name in Postgres
        ordering = ['-reserved_at']  # default ordering when you query .all()
        verbose_name = 'Reservation'
        verbose_name_plural = 'Reservations'

        # --- Indexes -------------------------------------------------
        # These MIRROR the indexes already created by the raw SQL below.
        # Because managed=False, Django will NOT emit CREATE INDEX
        # statements for these — they're purely documentation/metadata
        # here. Keeping them in sync with the SQL is a manual discipline,
        # not something Django enforces for unmanaged models.
        #
        #   CREATE INDEX idx_reservation_book_id   ON reservations(book_copy_id);
        #   CREATE INDEX idx_reservation_member_id ON reservations(member_id);
        #   CREATE INDEX idx_reservation_status     ON reservations(status);
        indexes = [
            models.Index(fields=['book_copy'], name='idx_reservation_book_id'),
            models.Index(fields=['member'], name='idx_reservation_member_id'),
            models.Index(fields=['status'], name='idx_reservation_status'),
        ]

    def __str__(self):
        # Used by Django admin and debugging — makes objects readable
        # instead of printing "Reservation object (1)".
        return f"Reservation {self.id} ({self.status})"