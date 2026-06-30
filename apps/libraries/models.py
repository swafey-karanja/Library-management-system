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
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    """

    # --- Primary Key ---
    # Postgres generates this UUID for us via `gen_random_uuid()` (pgcrypto
    # extension), so on the Django side we mark editable=False and don't
    # provide a default — we let the database handle ID generation.
    # We still declare it as a UUIDField so Django reads/writes UUID values
    # correctly when querying or serializing.
    library_id = models.UUIDField(
        primary_key=True,
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

    # logo, url, primary_color, secondary_color are all optional in SQL
    # (no NOT NULL constraint), so we set null=True so Django allows NULL
    # in the database, and blank=True so forms/serializers don't require
    # a value either.
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
    # default=dict means a new Library instance starts with an empty JSON
    # object ({}) rather than NULL, which is usually easier to work with
    # when checking "is module X enabled?" type logic later.
    enabled_modules = models.JSONField(
        null=True,
        blank=True,
        default=dict,
        help_text="JSON object describing which system modules are enabled for this library.",
    )

    # --- Timestamps ---
    # auto_now_add=True makes Django set this automatically the moment the
    # row is created, mirroring the SQL `DEFAULT CURRENT_TIMESTAMP`.
    # editable=False prevents this from showing up in forms.
    created_at = models.DateTimeField(
        auto_now_add=True,
        editable=False,
    )

    class Meta:
        # This tells Django: "this model does not own this table" — no
        # CREATE TABLE / ALTER TABLE / DROP TABLE will ever be generated
        # for it via `makemigrations` / `migrate`.
        managed = False

        # Explicitly point this model at the existing table name, since
        # Django would otherwise default to "libraries_library" (based on
        # app label + model name).
        db_table = "library"

        # Optional, but nice for Django admin and shell readability.
        verbose_name = "Library"
        verbose_name_plural = "Libraries"

        # Matches the SQL index `idx_library_name` — this doesn't create
        # the index (managed=False handles that), it just documents intent
        # and helps Django's query optimizer reasoning / admin tooling.
        indexes = [
            models.Index(fields=["name"], name="idx_library_name"),
        ]

    def __str__(self):
        # This controls how a Library instance is displayed as a string —
        # e.g. in the Django admin list view, or when printed in the shell.
        return self.name
