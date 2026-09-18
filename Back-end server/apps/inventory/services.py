"""
apps/inventory/services.py

Keeps the `inventory` table (per-library, per-book aggregate counts)
in sync with the real source of truth: individual rows in
`book_copies`. This implements the HYBRID strategy chosen for this
app:

  1. sync_inventory() / sync_inventory_for_copy() / sync_inventory_many()
     are called EXPLICITLY, right after any book_copies write that
     could change the counts — checkout, return, create, bulk-update,
     CSV import, reservation claim/release. This keeps numbers correct
     in real time for the overwhelming majority of requests, matching
     the same "explicit call, not a signal" convention already used by
     apps/reservations/services.py.

  2. reconcile_all_inventory() (see apps/inventory/tasks.py for the
     scheduled Celery job that calls it) recomputes EVERY inventory
     row from scratch. It's a safety net for the one thing explicit
     calls can never catch: deleting a Book or Library cascades onto
     book_copies entirely inside PostgreSQL (ON DELETE CASCADE) —
     Django never runs any Python for those rows individually, so
     nothing explicit ever fires for them.

DESIGN CHOICE — recompute, not increment:
    sync_inventory() always derives its numbers from a fresh aggregate
    query over book_copies; it never adds/subtracts a delta from the
    previous count. That makes it IDEMPOTENT and SELF-HEALING — calling
    it twice, out of order, or after a previous call was missed always
    leaves the row in the one genuinely correct state. A delta/F()-
    expression version would be marginally cheaper per call, but any
    missed or double-counted call would drift forever. Not worth that
    risk given how small a (library, book) group of copies actually is
    (typically single digits to a few dozen rows) — an indexed
    aggregate over that many rows is cheap every time.
"""
from django.db import transaction
from django.db.models import Count, Q

from .models import Inventory


def sync_inventory(library_id, book_id):
    """
    Recomputes and saves the inventory row for ONE (library, book)
    pair, from the CURRENT state of book_copies. Safe to call as often
    as you like — every call is authoritative, not additive.
    """
    # Imported inside the function, not at module level: book_copies
    # doesn't import inventory, so importing BookCopy lazily here
    # avoids forcing an import order between the two apps.
    from apps.book_copies.models import BookCopy

    with transaction.atomic():
        # select_for_update() locks (or creates) this ONE inventory
        # row, so two concurrent syncs for the same pair — e.g. a
        # checkout and a return landing at nearly the same instant —
        # serialize instead of racing to overwrite each other's result.
        inventory, _created = Inventory.objects.select_for_update().get_or_create(
            library_id=library_id,
            book_id=book_id,
        )

        counts = BookCopy.objects.filter(
            library_id=library_id, book_id=book_id
        ).aggregate(
            total=Count("id"),
            available=Count("id", filter=Q(status=BookCopy.STATUS_AVAILABLE)),
            borrowed=Count("id", filter=Q(status=BookCopy.STATUS_BORROWED)),
            reserved=Count("id", filter=Q(status=BookCopy.STATUS_RESERVED)),
        )

        inventory.total_copies = counts["total"] or 0
        inventory.available_copies = counts["available"] or 0
        inventory.borrowed_copies = counts["borrowed"] or 0
        inventory.reserved_copies = counts["reserved"] or 0
        inventory.save(
            update_fields=[
                "total_copies",
                "available_copies",
                "borrowed_copies",
                "reserved_copies",
                "updated_at",
            ]
        )

    return inventory


def sync_inventory_for_copy(book_copy):
    """
    Convenience wrapper for the common single-copy case — e.g. right
    after one checkout or return changes one book_copy's status.
    """
    return sync_inventory(book_copy.library_id, book_copy.book_id)


def sync_inventory_many(pairs):
    """
    Call once, after a BATCH of book_copies writes, with the set of
    DISTINCT (library_id, book_id) pairs touched during that batch —
    e.g. from a bulk-create, bulk-update, or CSV import. This is what
    keeps a 500-copy request down to a handful of sync calls (one per
    distinct book/library combination actually touched) instead of
    500 redundant recomputes of the same few rows.

    `pairs` is any iterable of (library_id, book_id) tuples, typically
    built with a set() while looping over the affected copies:

        touched_pairs = {(c.library_id, c.book_id) for c in copies}
        sync_inventory_many(touched_pairs)
    """
    for library_id, book_id in pairs:
        sync_inventory(library_id, book_id)


def reconcile_all_inventory():
    """
    Recomputes EVERY inventory row that should exist, from scratch.
    Used by both the scheduled Celery task (apps/inventory/tasks.py)
    and the `reconcile_inventory` management command, so there's a
    single definition of "correct" shared between the automatic and
    manual paths.

    Walks every DISTINCT (library, book) pair that currently has at
    least one book_copies row — not just pairs with an existing
    Inventory row — so a combination that has never been synced (its
    first copy added before this system existed, or a sync call that
    failed silently) still gets an inventory row created here.

    Returns the number of (library, book) pairs reconciled.
    """
    from apps.book_copies.models import BookCopy

    distinct_pairs = BookCopy.objects.values_list(
        "library_id", "book_id"
    ).distinct()

    reconciled = 0
    for library_id, book_id in distinct_pairs.iterator():
        sync_inventory(library_id, book_id)
        reconciled += 1

    return reconciled