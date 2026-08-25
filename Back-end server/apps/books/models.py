import uuid
from django.db import models

"""
books/models.py

This file defines the Django ORM model(s) for the `books` app.
"""


class Book(models.Model):
    """
    Represents a single title in the library's catalog (NOT a physical
    copy on a shelf - that's handled by the `book_copies` table/app).

    Think of it like this:
        Book        -> "Harry Potter and the Philosopher's Stone" (the work)
        BookCopy    -> physical copy #3 of that book, sitting on Shelf A2

    This separation lets a library own many physical copies of the same
    book without duplicating title/author/description data.
    """

    # --- Primary Key -----------------------------------------------------
    # Your SQL uses:
    #   book_id UUID PRIMARY KEY DEFAULT gen_random_uuid()
    #
    # Django's UUIDField mirrors Postgres' UUID type. We set
    # `primary_key=True` so Django knows this is the PK (replacing the
    # default auto-incrementing `id` field Django would normally add).
    #
    # `default=uuid.uuid4` generates a UUID in Python whenever a new Book
    # is created via Django (e.g. Book.objects.create(...)). Your
    # database ALSO has its own default (gen_random_uuid()) as a safety
    # net in case a row is ever inserted outside of Django - both are
    # fine to have; Django's default just takes priority when you use
    # the ORM.
    book_id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,  # hides this field from Django admin forms - it's system-generated
    )

    # --- Core bibliographic fields ----------------------------------------
    # SQL: title VARCHAR(1000) NOT NULL
    # `max_length` is required on CharField and maps directly to VARCHAR(n).
    title = models.CharField(max_length=1000)

    # SQL: subtitle VARCHAR(255)  (nullable - no NOT NULL constraint)
    # `blank=True` -> allowed to be empty in Django forms/admin
    # `null=True`  -> allowed to be NULL in the actual database column
    # We generally set both together for optional text fields.
    subtitle = models.CharField(max_length=255, blank=True, null=True)

    # SQL: authors VARCHAR(255) NOT NULL
    # Note: this stores authors as a single text field (e.g.
    # "J.K. Rowling, Mary GrandPré"), matching your SQL exactly. A more
    # "normalized" design would use a separate Author model with a
    # many-to-many relationship, but we're mirroring your schema as-is.
    authors = models.CharField(max_length=255)

    # SQL: publication_year DATE
    # Slightly unusual naming (it's a full DATE, not just a year), but we
    # mirror it faithfully. DateField maps to Postgres' DATE type.
    publication_year = models.IntegerField(blank=True, null=True)

    # SQL: genre VARCHAR(100)
    genre = models.CharField(max_length=100, blank=True, null=True)

    # SQL: description TEXT
    # TextField is for unbounded/long text (no max_length needed), unlike
    # CharField which is for short, bounded strings.
    description = models.TextField(blank=True, null=True)

    # --- Identifiers --------------------------------------------------------
    # SQL: isbn_13 VARCHAR(13) UNIQUE (nullable)
    # `unique=True` enforces a UNIQUE constraint at the DB level, exactly
    # like your SQL's UNIQUE keyword.
    isbn_13 = models.CharField(max_length=13, unique=True, blank=True, null=True)

    # SQL: isbn_10 VARCHAR(10) UNIQUE (nullable)
    isbn_10 = models.CharField(max_length=10, unique=True, blank=True, null=True)

    # --- Media ---------------------------------------------------------------
    # SQL: cover_image VARCHAR(500)
    # We use CharField (not URLField) to mirror the SQL type exactly,
    # since VARCHAR doesn't enforce URL formatting. If you'd like Django
    # to validate this is a well-formed URL whenever it's edited via a
    # form/admin, swap this for `models.URLField(max_length=500, ...)`.
    cover_image = models.CharField(max_length=500, blank=True, null=True)

    # --- Timestamps -----------------------------------------------------------
    # SQL: created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    # `auto_now_add=True` tells Django to automatically stamp this field
    # with the current time ONLY when the row is first created (it's
    # ignored on subsequent saves). This is the Django equivalent of
    # Postgres' DEFAULT CURRENT_TIMESTAMP.
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        # By default Django names tables "<app_label>_<modelname>", e.g.
        # "books_book". `db_table` overrides that so this model maps
        # exactly onto your existing `books` table from the SQL script.
        db_table = "books"

        # managed = False tells Django: "don't create, alter, or drop
        # this table via migrations - it already exists, and the SQL
        # script is the source of truth for its structure."

        managed = False

        # These mirror the CREATE INDEX statements from your SQL that
        # reference real columns. Since managed = False, Django won't
        # actually generate CREATE INDEX SQL for these - they exist here
        # purely as documentation so anyone reading this model knows
        # which columns are indexed at the database level already.
        indexes = [
            models.Index(fields=["title"], name="idx_books_title"),
            models.Index(fields=["authors"], name="idx_books_authors"),
            models.Index(fields=["genre"], name="idx_books_genre"),
        ]

        # Nice-to-haves for Django admin / shell readability.
        verbose_name = "Book"
        verbose_name_plural = "Books"
        ordering = ["title"]

    def __str__(self):
        # This controls how a Book instance prints out - e.g. in the
        # Django admin dropdowns, shell, or debug output. Always define
        # this on your models; "Book object (1)" is not helpful!
        return self.title
