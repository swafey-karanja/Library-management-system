"""
models.py — BookCopy model
===========================

This file defines the Django ORM representation of the `book_copies`
table that already exists in the PostgreSQL database (created by the
raw SQL schema you shared).

WHY managed = False?
---------------------
By default, Django "manages" a model's table: it creates it, alters it,
and drops it whenever you run `makemigrations` / `migrate`. Since this
table was already created manually via SQL (and even has its `status`
and `condition` columns converted to native Postgres ENUM types via
ALTER TABLE), we do NOT want Django touching the schema at all.

Setting `managed = False` in the model's Meta class tells Django:
  "This table exists already — just let me query/insert/update rows
   through it, but never generate migrations that create or modify it."

This is the standard pattern when Django sits on top of a pre-existing
or externally-managed database.
"""

import uuid
from django.db import models


class BookCopy(models.Model):
    """
    Represents a single physical copy of a Book.

    A `Book` (title, author, ISBN, etc.) is a catalog entry — think of
    it as "the idea of the book". A `BookCopy` is one physical item on
    a shelf: it has a barcode, a condition, a shelf location, and a
    status (available, borrowed, etc). A single Book can have many
    BookCopy rows — that's why book_copies has a foreign key to books.
    """

    # ------------------------------------------------------------------
    # PRIMARY KEY
    # ------------------------------------------------------------------
    # The SQL schema uses:
    #   id UUID PRIMARY KEY DEFAULT gen_random_uuid()
    #
    # `editable=False` hides this field from Django admin/forms, since
    # it's not something a user should type in manually.
    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )

    # ------------------------------------------------------------------
    # FOREIGN KEYS
    # ------------------------------------------------------------------
    # A ForeignKey creates a many-to-one relationship: many BookCopy rows
    # can point to a single Library / Book row.
    #
    # We reference the related models as STRINGS ('library.Library',
    # 'books.Book') rather than importing the classes directly. This is
    # called a "lazy reference" and is useful here because:
    #   1. It avoids circular imports between apps.
    #   2. The `library` and `books` apps can be built independently,
    #      in any order, and Django resolves the string at app-load time.
    #
    # `on_delete=models.CASCADE` mirrors the SQL `ON DELETE CASCADE`:
    # if the parent Library or Book row is deleted, all of its
    # BookCopy rows are deleted too. NOTE: because this table is
    # unmanaged, this CASCADE behaviour is actually enforced by
    # Postgres itself (via the FK constraint in your SQL). Setting
    # on_delete here mainly keeps Django's in-memory ORM behaviour
    # consistent with the database, and is required by Django syntax
    # regardless of managed=False.
    #
    # `db_column` tells Django the actual column name in Postgres,
    # since Django's convention would otherwise expect `library_id`
    # to be named `library` + `_id` automatically anyway — here it's
    # explicit for clarity and to guarantee an exact match with the
    # SQL schema (`library_id`, `book_id`).
    #
    # `related_name` lets you query backwards, e.g.
    #   some_library.copies.all()   -> all BookCopy rows for that library
    #   some_book.copies.all()      -> all BookCopy rows for that book
    library = models.ForeignKey(
        "libraries.Library",
        on_delete=models.CASCADE,
        db_column='library_id',
        related_name='book_copies',
    )

    book = models.ForeignKey(
        'books.Book',
        on_delete=models.CASCADE,
        db_column='book_id',
        related_name='copies',
    )

    # ------------------------------------------------------------------
    # SIMPLE FIELDS
    # ------------------------------------------------------------------
    # barcode VARCHAR(100) NOT NULL UNIQUE
    # `unique=True` enforces the SQL UNIQUE constraint at the Django
    # (application) level too, so you get a friendly ValidationError
    # before Postgres would otherwise raise an IntegrityError.
    barcode = models.CharField(max_length=100, unique=True)

    # ------------------------------------------------------------------
    # ENUM-BACKED FIELDS: status & condition
    # ------------------------------------------------------------------
    # In your SQL, these columns were converted from VARCHAR to native
    # Postgres ENUM types (`book_status`, `book_condition`) via:
    #   ALTER COLUMN status TYPE book_status USING status::book_status
    #
    # Django has no direct concept of a Postgres ENUM type — as far as
    # the ORM is concerned, it just sees/writes a text value into that
    # column, and Postgres itself enforces which values are legal.
    #
    # We use CharField with `choices=` to:
    #   1. Mirror the constraint at the Python/application level
    #      (so invalid values are caught early, with a clear error).
    #   2. Get a nice dropdown automatically in the Django admin.
    #
    # IMPORTANT: the values below (STATUS_CHOICES / CONDITION_CHOICES)
    # must exactly match the labels defined in your `CREATE TYPE
    # book_status AS ENUM (...)` / `CREATE TYPE book_condition AS ENUM
    # (...)` statements in the database. Update this list if your
    # actual enum values differ — Postgres will reject anything that
    # doesn't match its enum, regardless of what Django allows.
    STATUS_AVAILABLE = 'available'
    STATUS_BORROWED = 'checked_out'
    STATUS_RESERVED = 'reserved'
    STATUS_LOST = 'lost'
    STATUS_DAMAGED = 'damaged'
    STATUS_UNAVAILABLE = 'unavailable'

    STATUS_CHOICES = [
        (STATUS_AVAILABLE, 'Available'),
        (STATUS_BORROWED, 'Borrowed'),
        (STATUS_RESERVED, 'Reserved'),
        (STATUS_LOST, 'Lost'),
        (STATUS_DAMAGED, 'Damaged'),
        (STATUS_UNAVAILABLE, 'Unavailable'),
    ]

    CONDITION_NEW = 'new'
    CONDITION_GOOD = 'good'
    CONDITION_FAIR = 'fair'
    CONDITION_POOR = 'poor'
    CONDITION_DAMAGED = 'damaged'

    CONDITION_CHOICES = [
        (CONDITION_NEW, 'New'),
        (CONDITION_GOOD, 'Good'),
        (CONDITION_FAIR, 'Fair'),
        (CONDITION_POOR, 'Poor'),
        (CONDITION_DAMAGED, 'Damaged'),
    ]

    # max_length here refers to the longest possible *choice string*,
    # not a SQL column width — Postgres is enforcing the real type.
    status = models.CharField(
        max_length=30,
        choices=STATUS_CHOICES,
        default=STATUS_AVAILABLE,
    )

    condition = models.CharField(
        max_length=50,
        choices=CONDITION_CHOICES,
        default=CONDITION_NEW,
    )

    # shelf_location VARCHAR(100) — nullable in SQL (no NOT NULL clause)
    # `blank=True` -> allowed to be empty in Django forms/admin.
    # `null=True`  -> allowed to be NULL in the database.
    # We set both together because for CharFields, Django's own style
    # guide recommends avoiding NULL and using '' for "no value" —
    # but since the database column genuinely allows NULL here, we
    # mirror that with null=True to stay accurate to the real schema.
    shelf_location = models.CharField(max_length=100, blank=True, null=True)

    # acquired_at TIMESTAMP — nullable, no default in SQL.
    # DateTimeField is Django's mapping for a Postgres TIMESTAMP column.
    acquired_at = models.DateTimeField(blank=True, null=True)

    # created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    # `auto_now_add=True` makes Django automatically stamp "now" the
    # moment a row is first created via the ORM. This is a Python-side
    # equivalent of the database's DEFAULT CURRENT_TIMESTAMP — again,
    # both can coexist safely; whichever layer performs the INSERT will
    # supply the timestamp.
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        # Tells Django not to create/alter/drop this table — it already
        # exists in the database exactly as defined in your SQL schema.
        managed = False

        # Explicitly maps this model to the existing `book_copies`
        # table name (otherwise Django would guess `book_copies_bookcopy`
        # based on app_label + model name).
        db_table = 'book_copies'

        # Declaring indexes here is purely DOCUMENTATION when
        # managed=False — Django will NOT generate migrations to create
        # them (they already exist via your CREATE INDEX statements).
        # It's still useful to list them so anyone reading models.py can
        # see how the table is actually indexed in Postgres.
        indexes = [
            models.Index(fields=['library']),          # idx_book_copies_library_id
            models.Index(fields=['book']),              # idx_book_copies_book_id
            models.Index(fields=['status']),             # idx_book_copies_status
            models.Index(fields=['shelf_location']),     # idx_book_copies_shelf_location
            models.Index(
                fields=['library', 'book', 'status'],
                name='idx_book_copies_lookup',
            ),
        ]

        verbose_name = 'Book Copy'
        verbose_name_plural = 'Book Copies'

    def __str__(self):
        # __str__ controls how an instance of this model prints —
        # e.g. in the Django admin list view or when you do print(obj).
        # A barcode is the most human-recognizable identifier for a
        # single physical copy, so we use that instead of the raw UUID.
        return f'Copy {self.barcode} ({self.status})'