from django.db import models
import uuid


class Inventory(models.Model):
    """
    Maps to the `inventory` table (raw SQL, not managed by Django migrations).

    This table is a per-(library, book) COUNTER/CACHE table — it stores
    aggregate numbers (how many total/available/borrowed/reserved copies
    a given library has of a given book) so the app doesn't have to run
    COUNT(*) queries against `book_copies` every time it needs this info.
    In practice, your application logic (or a DB trigger, if you add one
    later) is responsible for keeping these numbers in sync whenever a
    book_copies row's status changes.
    """

    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,  # hides this field from Django admin forms - it's system-generated
    )

    library = models.ForeignKey(
        "libraries.Library",
        on_delete=models.CASCADE,   # mirrors ON DELETE CASCADE in the SQL FK
        db_column="library_id",
        related_name="inventory",
    )

    book = models.ForeignKey(
        "books.Book",
        on_delete=models.CASCADE,
        db_column="book_id",
        related_name="inventory",
    )

    # --- FIX 1: added default=0 to match `DEFAULT 0` in the SQL schema.
    # Without a default, creating Inventory() from Django without explicitly
    # passing these values would try to insert NULL (or raise a Python-side
    # error because null=False), instead of falling back to 0 like the SQL
    # DEFAULT does. PositiveIntegerField also stops negative counts at the
    # Python/validation level (the SQL doesn't enforce this, so it's a
    # helpful extra safety net purely on the Django side).
    total_copies = models.PositiveIntegerField(
        default=0,
        blank=False,
        null=False,
        db_column="total_copies",
    )

    available_copies = models.PositiveIntegerField(
        default=0,
        blank=False,
        null=False,
        db_column="available_copies",
    )

    borrowed_copies = models.PositiveIntegerField(
        default=0,
        blank=False,
        null=False,
        db_column="borrowed_copies",
    )

    reserved_copies = models.PositiveIntegerField(
        default=0,
        blank=False,
        null=False,
        db_column="reserved_copies",
    )

    # --- FIX 2: auto_now_add -> auto_now.
    # auto_now_add=True only sets the timestamp ONCE, when the row is first
    # created (like created_at). Since this field is called `updated_at`,
    # it should refresh every time the row is saved -- that's what
    # auto_now=True does. Note: the raw SQL only has `DEFAULT CURRENT_TIMESTAMP`
    # (no ON UPDATE trigger), so the DB itself won't auto-refresh this column
    # either -- auto_now=True makes Django set it correctly on every
    # `.save()` call made through the ORM. If you ever update rows via raw
    # SQL or bulk_update() without listing this field, it will NOT be
    # refreshed automatically, since that bypasses Django's save() logic.
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        # managed = False tells Django "don't create/alter/drop this table,
        # I already created it myself with raw SQL". Because of this,
        # Django migrations will NEVER create the indexes listed below --
        # they are effectively just documentation here, describing indexes
        # that already exist in the DB (created by your SQL script).
        # If you ever flip managed=True, Django would try to actually
        # create these indexes/table via a migration.
        managed = False
        db_table = "inventory"

        indexes = [
            models.Index(fields=["library"]),  # idx_inventory_library_id
            models.Index(fields=["book"]),  # idx_inventory_book_id
            models.Index(
                fields=["library", "book"],
                name="idx_inventory_lookup",
            ),
        ]

        # --- FIX 3: enforce "one inventory row per (library, book) pair"
        # at the database level, not just by convention. This does NOT
        # replace idx_inventory_lookup (that index still exists in the DB
        # and speeds up lookups) -- it ADDS a uniqueness rule on top of it.
        # Since this model is managed=False, adding this here does NOT
        # automatically create the constraint in the DB (see chat for the
        # migration steps needed to actually apply it).
        constraints = [
            models.UniqueConstraint(
                fields=["library", "book"],
                name="uq_inventory_library_book",
            )
        ]

        verbose_name = "Inventory"
        verbose_name_plural = "Inventories"

    # --- FIX 4: __str__ makes objects readable in the Django admin and in
    # the shell (e.g. when debugging with `Inventory.objects.first()`),
    # instead of showing an unhelpful "Inventory object (uuid)".
    def __str__(self):
        return f"{self.book_id} @ {self.library_id} ({self.available_copies}/{self.total_copies} available)"