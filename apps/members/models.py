"""
models.py -- MEMBERS module

This file defines the Django ORM representation of the `members` table
that already exists in our PostgreSQL database (created via raw SQL,
see the schema at the top of the project notes).
"""

import uuid
from django.db import models


class Member(models.Model):
    """
    Represents a single row in the `members` table.

    Each member belongs to exactly one library (multi-tenant style setup,
    since this whole system supports multiple libraries sharing one DB).
    """

    # --- Primary Key ---
    # The SQL schema defines `member_id UUID PRIMARY KEY DEFAULT gen_random_uuid()`.
    # We mirror that here:
    #   - primary_key=True marks this as the table's primary key column.
    #   - default=uuid.uuid4 lets Django generate a UUID on the Python side
    #     if a row is created without the DB default kicking in (e.g. via
    #     Django ORM .create()). The DB-side default (gen_random_uuid())
    #     still exists as a safety net for raw SQL inserts.
    #   - editable=False hides it from Django admin/forms since it should
    #     never be manually typed in by a user.
    member_id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
        db_column="member_id",
    )

    # --- Foreign Key to the library table ---
    # `library_id UUID NOT NULL REFERENCES library(library_id) ON DELETE CASCADE`
    #
    # We point this ForeignKey at the Library model, which we assume lives
    # in a separate Django app called `library` (module built earlier).
    # Update the "library.Library" string below if your app/model is named
    # differently.
    #
    #   - to_field="library_id" tells Django which column on the Library
    #     model this FK actually references (Library's own primary key).
    #   - db_column="library_id" keeps the column name identical to the
    #     one already in the database (Django would otherwise default to
    #     "library_id_id" or similar).
    #   - on_delete=models.CASCADE mirrors the SQL "ON DELETE CASCADE":
    #     if a library is deleted, all of its members are deleted too.
    #   - related_name lets us do `some_library.members.all()` from the
    #     Library side.
    library = models.ForeignKey(
        "libraries.Library",
        to_field="library_id",
        db_column="library_id",
        on_delete=models.CASCADE,
        related_name="members",
    )

    # --- Membership number ---
    # `membership_no VARCHAR(50) NOT NULL UNIQUE`
    # This is the human-facing library card / membership ID, distinct
    # from the internal UUID primary key. unique=True enforces the SQL
    # UNIQUE constraint at the Django level as well (Django will validate
    # this before hitting the DB, in addition to the DB itself enforcing it).
    membership_no = models.CharField(
        max_length=50,
        unique=True,
        db_column="membership_no",
    )

    # --- Basic member details ---
    # `name VARCHAR(120) NOT NULL`
    name = models.CharField(max_length=120, db_column="name")

    # `email VARCHAR(255)` -- not nullable in the DB (NOT NULL constraint),
    # so we set blank=False (not allowed to be empty in forms) and null=False
    # (not allowed to be NULL in the DB) to match.
    email = models.CharField(
        max_length=255,
        blank=False,
        null=False,
        db_column="email",
    )

    # `phone VARCHAR(20)` -- not nullable/optional.
    phone_number = models.CharField(
        max_length=20,
        blank=False,
        null=False,
        db_column="phone_number",
    )

    # `address VARCHAR(255)` -- also nullable/optional.
    address = models.CharField(
        max_length=255,
        blank=True,
        null=True,
        db_column="address",
    )

    # --- Status flag ---
    # `status BOOLEAN DEFAULT TRUE`
    # Represents whether the membership is currently active. TRUE = active.
    status = models.BooleanField(default=False, db_column="status")

    # --- Timestamps ---
    # `created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP`
    # auto_now_add=True makes Django set this automatically the first time
    # the row is created, and never touch it again afterwards. Note: since
    # the DB itself also has a DEFAULT CURRENT_TIMESTAMP, this is really
    # just here so Django is "aware" of the column and can read/display it.
    created_at = models.DateTimeField(auto_now_add=True, db_column="created_at")

    class Meta:
        # Tell Django exactly which physical table this model maps to.
        # Without this, Django would guess "members_member" based on
        # app label + model name, which doesn't match our real table.
        db_table = "members"

        # As explained at the top of this file: don't let Django create,
        # alter, or drop this table. It already exists and is managed by
        # our hand-written SQL migrations/schema.
        managed = False

        # Nice-to-haves for the Django admin site and shell output.
        verbose_name = "Member"
        verbose_name_plural = "Members"
        ordering = ["name"]

    def __str__(self):
        # This controls how a Member instance prints in the Django admin,
        # shell, and anywhere else `str(member)` is used. Very useful
        # while debugging.
        return f"{self.name} ({self.membership_no})"