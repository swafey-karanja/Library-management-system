# book_copies

## Purpose

Tracks individual **physical copies** of books held by libraries. A `Book` is a
catalog entry (title, author, ISBN — "the idea of the book"); a `BookCopy` is
one physical item on a shelf at a specific library, with its own barcode,
condition, shelf location, and status (available, borrowed, lost, etc). One
`Book` can have many `BookCopy` rows across one or more libraries.

## Key Models

- **`BookCopy`** — one physical copy of a book.
  - `id` — UUID primary key (client never supplies this).
  - `library` (FK -> `libraries.Library`, `related_name='book_copies'`),
    `book` (FK -> `books.Book`, `related_name='copies'`) — both `CASCADE` on delete.
  - `barcode` — unique; if omitted, auto-generated in `save()` from the book's
    ISBN-13 (preferred), then ISBN-10, then a title slug, plus a zero-padded
    sequence number (e.g. `9780134685991-0004`); collision-checked in a loop.
  - `status` — choices: `available` / `checked_out` / `reserved` / `lost` /
    `damaged` / `unavailable` (default `available`).
  - `condition` — choices: `new` / `good` / `fair` / `poor` / `damaged`
    (default `new`).
  - `shelf_location` (nullable), `acquired_at` (nullable — `save()` fills it
    with "now" if left blank on create), `created_at` (auto-set once).
  - `save()` override handles both the barcode auto-generation and the
    `acquired_at` default — this is where that business logic lives, not in
    views/serializers.
  - `Meta.managed = False`, `db_table = 'book_copies'` — the table already
    exists in Postgres (raw SQL, with native enum types for `status`/
    `condition`); Django never creates/alters/drops it via migrations.
    Indexed on `library`, `book`, `status`, `shelf_location`, and the
    composite `(library, book, status)`.

## API Endpoints

Mounted under `/api/book-copies/`. **All endpoints require
`IsAdminOrLibrarian`** (`core.permissions`) — no per-endpoint variation.

- `GET /` — list copies. Query params: `search=` (space-separated terms,
  ANDed, matched against barcode/book title/library name/shelf location),
  `status=`, `condition=`, `library=<uuid>`, `book_name=` (partial title
  match), `ordering=acquired_at|-acquired_at|created_at|-created_at`,
  `page=`/`page_size=` (default 20, max 100). Librarians only see their own
  library's copies; admins see all.
- `GET /<identifier>/` — retrieve one copy by **either** UUID or barcode
  (same endpoint, disambiguated by trying to parse a UUID). 404 if the copy
  belongs to another library and the requester isn't an admin.
- `POST /add/` — create one copy (single JSON object body) or many (array
  body). Each item may set `library`, `book`, optional `barcode` (only
  allowed when `quantity` is 1 — rejected otherwise), optional `quantity`
  (default 1), and optional `status`/`condition`/`shelf_location`/
  `acquired_at`. All-or-nothing (wrapped in one transaction); total copies
  requested across the whole call is capped at 500. Response is a single
  object if exactly one copy was created from a non-batch request, a list
  otherwise (so `quantity > 1` on a single-object request still returns a
  list). Non-admins get a 403 if any spec's `library` isn't their own.
- `PUT`/`PATCH /<uuid:pk>/update/` — full/partial update of one copy.
  Library-scoped (404, not 403, if the pk belongs to another library).
- `PATCH /bulk-update/` — update many copies at once. Body: list of
  `{"id": "<uuid>", ...}`, where each item must include at least one of
  `status`/`condition`/`shelf_location`/`acquired_at` (barcode/library/book
  can't be changed this way). Duplicate ids in one request are rejected.
  Ids belonging to another library are reported the same as genuinely
  missing ids (no "exists but not yours" leak). Wrapped in one transaction.
- `GET /stats/` — aggregate counts, scoped like the list endpoint: total
  copies, `by_status`/`by_condition` (every choice present, zero-filled),
  `by_library` (count per library, admins only), `copies_acquired_last_30_days`,
  `copies_missing_shelf_location` (null or empty string).
- `GET /export/?format=csv|json` — downloads the same (filtered/searched/
  ordered) result set as the list endpoint, as an attachment.
- `POST /import/` — multipart upload, field name `file`, `.csv` only,
  UTF-8 (BOM-tolerant). Required columns: `library`, `book`, `barcode`;
  optional: `status`, `condition`, `shelf_location`, `acquired_at` (a blank
  cell means "leave unchanged" on update, not "clear it"). Upserts by
  barcode: an existing barcode updates that row (scoped, so a non-admin
  can't touch another library's copy even on a barcode match); no match
  creates a new one (the row's `library` column must equal the requester's
  own library id if non-admin). Best-effort — each row is saved in its own
  transaction, so valid rows persist even if others fail; response reports
  `created`/`updated` counts plus a per-row `errors` list (1-indexed to
  match a spreadsheet's row numbers, header excluded).

## Notes

- **Serializers**: `BookCopySerializer` (`ModelSerializer`) exposes an
  explicit field whitelist (not `__all__`) plus read-only `library_name`/
  `book_title` convenience fields reached through the FKs; `id`/`created_at`
  are read-only, `barcode` is optional (server-generated if omitted).
  `BookCopyBulkUpdateItemSerializer` and `BookCopyCreateSpecSerializer` are
  plain `Serializer`s (not tied 1:1 to a model row) used only for validating
  the bulk-update and create request bodies described above.
- **Library scoping** is the recurring theme across every view: enforced via
  `core.permissions`' `LibraryScopedQuerysetMixin` (list/detail/update/export),
  `scope_queryset_to_library()` (bulk-update/import/statistics), and
  `get_user_library_id()` (validating a `library` value on create/import).
  Admins are unrestricted; librarians are always confined to their own library.
- No signals in this app — side effects (barcode/`acquired_at` generation)
  live entirely in `BookCopy.save()`.
- Depends on `books` (for `Book`, and its ISBN fields used to build barcodes)
  and `libraries` (for `Library`) — both referenced as lazy string FKs
  (`'books.Book'`, `'libraries.Library'`) to avoid circular imports.
- `admin.py` has no models registered.
