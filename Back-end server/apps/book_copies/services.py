"""
apps/book_copies/services.py

Shared helper for changing a book_copy's status. Centralizes the
"change status -> save -> keep inventory in sync" sequence that was
previously duplicated by hand across every call site in
borrow_transactions and reservations that flips a copy's status —
same "explicit call into a services.py" convention already
established by apps/reservations/services.py.

NOT used by the book_copies app's OWN views (create / update /
bulk-update / CSV import). Those don't fit this helper's shape:
  - create doesn't change an EXISTING copy's status at all
  - bulk-update and CSV import touch several possibly-unrelated
    fields per row via a generic setattr()/serializer, not a
    dedicated status change
  - the single-item update view goes through DRF's serializer.save(),
    not a direct attribute assignment
Those views keep calling sync_inventory_many()/sync_inventory_for_copy()
directly (see apps/inventory/services.py) instead.
"""
from apps.inventory.services import sync_inventory_for_copy


def set_copy_status(book_copy, new_status, sync=True):
    """
    Sets book_copy.status, saves ONLY that field, and — by default —
    immediately syncs the matching inventory row. Returns book_copy.

    sync=False exists for exactly one situation: a caller looping
    over MULTIPLE, possibly different, copies that wants to dedupe
    the inventory sync itself afterwards with sync_inventory_many()
    instead of syncing once per copy (see BorrowBatchCheckoutView).
    Every other call site should leave sync=True (the default) and
    not have to think about it at all.
    """
    book_copy.status = new_status
    book_copy.save(update_fields=["status"])
    if sync:
        sync_inventory_for_copy(book_copy)
    return book_copy