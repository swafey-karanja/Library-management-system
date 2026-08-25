# Books App

## Purpose
Manages the library's **catalog of titles** — the bibliographic record for a book (title, author, ISBNs, genre, description, cover image), as distinct from any physical copy sitting on a shelf. Physical inventory/copies are conceptually a separate concern (see the `Book` docstring's `Book` vs `BookCopy` distinction) — this app is the "what is this book" layer that the rest of the system would reference, though nothing in this app's code currently holds an actual FK to a copies table.

## Key Model: `Book`
Maps onto an existing, **unmanaged** (`managed = False`) Postgres table `books` — Django never creates/alters this table via migrations; the SQL script is the source of truth.

- `book_id` — UUID primary key, generated in Python (`default=uuid.uuid4`), not editable via forms/admin.
- `title` (required), `subtitle` (optional), `authors` — a single free-text string (e.g. `"J.K. Rowling, Mary GrandPré"`), *not* a related `Author` model or FK.
- `isbn_13` (13 chars, **unique**), `isbn_10` (10 chars, **unique**) — both optional but enforce DB-level uniqueness when present.
- `genre` (free text, up to 100 chars — not a choices/enum field), `publication_year` (integer), `description` (long text), `cover_image` (plain string URL, not a validated `URLField`).
- `created_at` — auto-stamped on creation (`auto_now_add`); no `updated_at`.
- Indexed columns (documented, not Django-managed since the table is unmanaged): `title`, `authors`, `genre`.
- Default ordering: by `title`. Only model logic is `__str__` (returns `title`) — no other custom methods/properties, and no signals anywhere in the app.

## API Endpoints
All routes are mounted under whatever prefix `core/urls.py` gives this app (e.g. `api/books/`). **Every endpoint requires `IsAdminUser`** (from `core.permissions`) — there's no public or member-level read access anywhere in this app.

- **`GET /`** — list books.
  - `?search=<term>` — OR-search across `title`, `subtitle`, `isbn_10`, `isbn_13`, `authors` (multiple words ANDed).
  - Field filters (AND'd together, from `BookFilter`): `title`, `subtitle`, `authors`, `isbn_10`, `isbn_13` (all tokenized/punctuation-insensitive), `genre` (strict case-insensitive exact match), `publication_year`, `publication_year_min`/`publication_year_max`, `created_at_after`/`created_at_before`.
  - `?ordering=` — one of `publication_year`, `title`, `created_at` (prefix `-` for descending); defaults to `title`.
  - Paginated at 50/page (`MemberPagination`, fixed page size, not client-adjustable).
- **`POST /add/`** — create a book. Body = `Book` fields (`title`, `authors` required by the model; the rest optional). `book_id`/`created_at` are server-generated and ignored if sent.
- **`GET /stats/`** — no params; returns total count, publication-year min/max/avg, a genre → count breakdown, and counts of rows missing `isbn_13`/`isbn_10`/`description`.
- **`GET /export/`** — no params; streams the full catalog as a CSV file download (`Content-Disposition: attachment`), using `.iterator()` so it doesn't load the whole table into memory.
- **`POST /import/`** — `multipart/form-data` with a `file` field (CSV, same columns as export minus `book_id`/`created_at`). Each row is validated through `BookSerializer` individually; bad rows are skipped and returned in `failed_rows` with per-row errors rather than aborting the whole import. Responds `201` if at least one row was created, `400` if none were.
- **`PATCH /bulk-update/`** — body `{"book_ids": [...], "genre": "...", "publication_year": ...}`; `genre`/`publication_year` are both optional but at least one is required (enforced in `BookBulkUpdateSerializer.validate`). Applies via a single `.update()` call (bypasses `Model.save()`, so this would skip any future signals/save-time logic — not an issue today since `Book` has none). Response reports `updated_count` and any `not_found_ids` for IDs that didn't match an existing book.
- **`PATCH`/`PUT /<uuid:book_id>/update/`** — update one book, looked up by `book_id` (view overrides `lookup_field`/`lookup_url_kwarg` since DRF defaults to `pk`). Supports partial (`PATCH`) or full (`PUT`) updates; `book_id`/`created_at` are read-only and ignored.

## Notes
- **ISBN validation**: `BookSerializer.validate_isbn_13`/`validate_isbn_10` strip out hyphens/spaces before saving (so `"978-3-16-148410-0"` becomes the raw 13-digit string) and enforce exact length — 13 digits for ISBN-13, 10 chars for ISBN-10 (which may end in `X`, the valid ISBN-10 check character). This runs on both create and update.
- **Manual `book_id`/`update()` handling**: because the table is unmanaged, `BookSerializer.create()` generates `book_id` in Python rather than relying on the DB's `gen_random_uuid()` default, and `update()` sets fields individually (`instance.x = validated_data.get("x", instance.x)`) rather than using DRF's default bulk-`setattr`, so PATCH-style partial updates behave correctly.
- Search vs. filter are two distinct tools on the same list endpoint: `?search=` is a single free-text OR-match; the `title`/`authors`/etc. params are precise, AND-combinable, per-field filters (see `filters.py`).
- No cross-app FKs or imports beyond `core.permissions` — this app doesn't currently reference `book_copies`, users, or loans models directly.
