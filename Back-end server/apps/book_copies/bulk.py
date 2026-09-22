"""
bulk.py — helper for updating many rows with bulk_update() WITHOUT the
"lost update" bug that a naive implementation runs into.

THE BUG THIS FIXES
-------------------
Django's `Model.objects.bulk_update(objects, fields=[...])` writes
EVERY field named in `fields` for EVERY object passed in — not just
the fields that actually changed. So if a caller wants a single
bulk_update() call to cover a batch where each row changed different
columns, there are two options:

  (a) Pass `fields=ALL_POSSIBLE_FIELDS` for every row, first copying
      each row's *current* value into any field it didn't actually
      change (so writing it back is a no-op).

  (b) Group rows by which fields they actually changed, and issue one
      bulk_update() call per group, passing only that group's fields.

This module implements (b). Approach (a) has a real bug: the "current
value" it copies in was read at the START of the request. If another
request changes that same row in between — e.g. a member checks a
copy out, flipping its status — that change is silently overwritten
the moment this batch's bulk_update() fires, even though this batch
never touched status. That's a lost update, and it happened in
practice during testing of the book_copies bulk-update and import
views.

Approach (b) makes this class of bug impossible: a field that was
never in `fields=` for a given bulk_update() call can never be
written by that call, concurrent change or not. The trade-off is more
than one UPDATE statement when a batch mixes different combinations
of changed fields — still far fewer than one per row in the typical
case where most rows in a batch touch the same columns.
"""

from collections import defaultdict


def bulk_update_grouped_by_fields(model, rows, batch_size=None):
    """
    Write many rows via bulk_update(), grouped so each call only names
    the fields that group's rows actually changed.

    `rows` is an iterable of (instance, field_names) pairs:
        - `instance` is a model instance with its pk set and only its
          CHANGED attributes already assigned (leave untouched
          attributes alone — they are never read or written).
        - `field_names` is the collection of field names that were
          actually changed on that instance.

    Rows with no changed fields are skipped entirely (nothing to
    write). Returns the number of rows written.

    Example: 3 rows where two only changed `status` and one changed
    `status` AND `shelf_location` produces two bulk_update() calls —
    one with fields=['status'] for the first two rows, one with
    fields=['shelf_location', 'status'] for the third — instead of one
    call that also rewrites the third row's untouched `condition` and
    `acquired_at` back to whatever was read at the start of the
    request.
    """
    # Group rows by their exact set of changed fields. A dict keyed by
    # a sorted tuple (hashable, and order doesn't matter for the SQL)
    # keeps rows with the same field signature together.
    groups = defaultdict(list)
    for instance, field_names in rows:
        key = tuple(sorted(set(field_names)))
        if key:  # skip rows where nothing actually changed
            groups[key].append(instance)

    updated_count = 0
    for field_names, instances in groups.items():
        # bulk_update requires `fields` to be a list/tuple, not a set
        # or a plain dict_keys view — list(...) guarantees that.
        model.objects.bulk_update(instances, fields=list(field_names), batch_size=batch_size)
        updated_count += len(instances)

    return updated_count