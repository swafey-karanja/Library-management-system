import uuid
from django.db import models


class Library(models.Model):
    """
    Represents a single library tenant in the system.

    This model maps directly onto the 'library' table that was already
    created in PostgreSQL via raw SQL (see the schema script). Because the
    table already exists and is managed outside of Django's migration
    system, we set `managed = False` on the Meta class below — Django will
    read/write to this table, but will NEVER try to create, alter, or drop
    it through migrations. Any future schema changes must be made directly
    in PostgreSQL (matching the rest of the project's approach).

    Field-by-field, this mirrors the SQL definition:

        library_id UUID PRIMARY KEY DEFAULT gen_random_uuid()
        name VARCHAR(150) NOT NULL UNIQUE
        logo VARCHAR(500)
        url VARCHAR(255) UNIQUE
        primary_color VARCHAR(20)
        secondary_color VARCHAR(20)
        enabled_modules JSONB
        status VARCHAR(20) NOT NULL DEFAULT 'active'
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    """

    # --- Status choices ---
    # Defining allowed values as class-level constants rather than bare
    # strings serves two purposes:
    #   1. Django uses them to validate incoming data at the model/form
    #      level (in addition to the CHECK constraint in Postgres).
    #   2. They give us a single source of truth — if we ever need to
    #      reference these values elsewhere in code (e.g. filtering only
    #      active libraries), we write Library.Status.ACTIVE rather than
    #      the raw string "active", which makes typos impossible and
    #      refactoring easier.
    class Status(models.TextChoices):
        # TextChoices generates a tuple of (db_value, human_readable_label)
        # for each member. ACTIVE produces ("active", "Active") and
        # INACTIVE produces ("inactive", "Inactive"). Django stores the
        # db_value in the database and uses the label in admin/forms.
        ACTIVE = "active", "Active"
        INACTIVE = "inactive", "Inactive"

    # --- Primary Key ---
    # default=uuid.uuid4 generates a UUID on the Python side the moment
    # a new Library instance is created, before any SQL is sent. This is
    # necessary because managed=False means Django can't rely on the
    # database's DEFAULT gen_random_uuid() being triggered through the ORM.
    library_id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )

    # --- Core fields ---
    # max_length values match the VARCHAR lengths defined in SQL exactly.
    # unique=True mirrors the UNIQUE constraints in the schema.
    name = models.CharField(
        max_length=150,
        unique=True,
        null=False,
        blank=False,
        help_text="The display name of the library. Must be unique across the system.",
    )

    logo = models.CharField(
        max_length=500,
        null=True,
        blank=True,
        help_text="URL or path to the library's logo image.",
    )

    url = models.CharField(
        max_length=255,
        unique=True,
        null=True,
        blank=True,
        help_text="The library's public-facing URL/subdomain, if any.",
    )

    primary_color = models.CharField(
        max_length=20,
        null=True,
        blank=True,
        help_text="Primary branding color (e.g. hex code) for this library's theme.",
    )

    secondary_color = models.CharField(
        max_length=20,
        null=True,
        blank=True,
        help_text="Secondary branding color (e.g. hex code) for this library's theme.",
    )

    # --- JSON field ---
    # Maps to the JSONB column in Postgres. Django's JSONField handles the
    # conversion between Python dicts/lists and JSON automatically.
    enabled_modules = models.JSONField(
        null=True,
        blank=True,
        default=dict,
        help_text="JSON object describing which system modules are enabled for this library.",
    )

    # --- Status field ---
    # Uses the Status choices class defined above. choices=Status.choices
    # tells Django the only valid values are "active" and "inactive".
    # default=Status.ACTIVE means every newly created library starts as
    # active unless explicitly set otherwise — this mirrors the DEFAULT
    # 'active' we added to the Postgres column via ALTER TABLE.
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.ACTIVE,
        null=False,
        blank=False,
        help_text="Whether this library is currently active or inactive.",
    )

    # --- Timestamps ---
    # auto_now_add=True makes Django set this automatically the moment the
    # row is created, mirroring the SQL `DEFAULT CURRENT_TIMESTAMP`.
    created_at = models.DateTimeField(
        auto_now_add=True,
        editable=False,
    )

    class Meta:
        managed = False
        db_table = "library"
        verbose_name = "Library"
        verbose_name_plural = "Libraries"
        indexes = [
            models.Index(fields=["name"], name="idx_library_name"),
        ]

    def __str__(self):
        return self.name
