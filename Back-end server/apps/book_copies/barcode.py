"""
barcode.py — pure helpers for building and reading copy barcodes.

"Pure" means these functions only take values in and return values
out: they never touch the database. That makes them trivially easy to
unit-test, and it lets the create view build hundreds of barcodes in
memory and save them with ONE bulk INSERT.

BARCODE FORMAT
--------------
    <ISBN or title-slug>-<LIBRARY_CODE>-<4-digit sequence>
    e.g.  9780132350884-MAIN-0001

We call everything before the final "-NNNN" the PREFIX:
    9780132350884-MAIN

Every copy of the same book in the same library shares a prefix and
differs only by its sequence number.
"""


def build_prefix(isbn: str | None, title: str, library_code: str) -> str:
    """Return '<ISBN-or-slug>-<LIBRARY_CODE>' (no sequence number).

    The book part is the ISBN if the book has one, otherwise an
    8-character slug of the title, otherwise the literal 'BOOK'.
    """
    book_part = isbn or _slug(title) or 'BOOK'
    return f'{book_part}-{library_code}'


def format_barcode(prefix: str, sequence: int) -> str:
    """Attach a zero-padded sequence number to a prefix.

    zfill(4) pads to at least 4 digits ('7' -> '0007'). It never
    truncates, so sequence 10000 simply becomes '10000'.
    """
    return f'{prefix}-{str(sequence).zfill(4)}'


def build_barcode(isbn: str | None, title: str, library_code: str, sequence: int) -> str:
    """Convenience wrapper: prefix + sequence in one call."""
    return format_barcode(build_prefix(isbn, title, library_code), sequence)


def highest_sequence(existing_barcodes, prefix: str) -> int:
    """Return the largest sequence number already used under `prefix`.

    Returns 0 when there are none, so `highest_sequence(...) + 1` is
    always the correct next number.

    WHY "highest + 1" AND NOT "COUNT + 1"?
    Counting breaks as soon as a copy is deleted. With copies 1..5 and
    copy 2 deleted, COUNT is 4, so COUNT + 1 = 5 — a barcode that
    already exists. The highest number in use is 5, so highest + 1 = 6,
    which is always free. Gaps in the numbering are harmless.

    Barcodes that do not match '<prefix>-<digits>' exactly (custom
    barcodes typed in by a librarian, or imported from a CSV) are
    ignored rather than causing an error.
    """
    start = len(prefix) + 1          # skip past '<prefix>-'
    highest = 0
    for barcode in existing_barcodes:
        if not barcode.startswith(prefix + '-'):
            continue
        tail = barcode[start:]
        # isascii() guards against exotic Unicode digits that
        # isdigit() would accept but int() would misread.
        if tail.isascii() and tail.isdigit():
            highest = max(highest, int(tail))
    return highest


def _slug(title: str) -> str:
    """Uppercase letters/digits only, first 8 characters."""
    return ''.join(c for c in (title or '').upper() if c.isalnum())[:8]