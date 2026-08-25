# Reservations App

## Purpose

Manages members "holding" a specific physical book copy that isn't currently
available for checkout. A member reserves a `BookCopy`, which locks it to
them (status flips to `reserved`) until they collect it, cancel the hold, or
it expires and is released back into the available pool.

## Key Models

- **Reservation** — a single hold on a book copy by a member.
  - `id` — UUID PK (client-side default via `uuid.uuid4`, but the DB column has its own `gen_random_uuid()` default too)
  - `book_copy` (FK to `book_copies.BookCopy`, `on_delete=CASCADE`, `related_name='reservations'`) — the copy being held
  - `member` (FK to `members.Member`, `on_delete=CASCADE`, `related_name='reservations'`) — who holds it
  - `status` — one of `reserved` / `checked_out` / `cancelled` / `expired` (default `reserved`); the underlying Postgres table also enforces this via a DB-level CHECK constraint, Django's `choices` just mirrors it for validation/admin display
  - `reserved_at`, `expires_at` — DB-defaulted timestamps (Postgres fills `reserved_at` via `CURRENT_TIMESTAMP` if omitted); no model-level methods beyond `__str__`
  - `Meta.managed = False` — this model maps onto an existing table; Django won't create/alter/drop it, and the declared `indexes` (on `book_copy`, `member`, `status`) are documentation only, not something migrations will emit
  - Default ordering: `-reserved_at`

## API Endpoints

All endpoints use DRF's project-wide default permission, `IsAuthenticated` — the app defines no per-view `permission_classes` override.

- **`GET /`** — list reservations.
  - Filters (`ReservationFilter`): `status` (must be a valid choice or 400), `book_copy` / `member` (UUIDs), `reserved_after` / `reserved_before` / `expires_after` / `expires_before` (datetime range filters)
  - Free-text `search` across `member__name` and `member__email`
  - Paginated (`page`, `page_size`; see below)
  - Response uses `ReservationListSerializer` (read-only), including denormalized `member_name` and `book_copy_barcode` convenience fields
- **`POST /`** — create a reservation.
  - Body: `book_copy` (UUID, required), `member` (UUID, required), `expires_at` (optional — defaults to `reserved_at + 3 days` if omitted)
  - Validation: rejects with 400 if `book_copy.status != BookCopy.STATUS_AVAILABLE`
  - Side effect: on success, immediately flips the target `BookCopy.status` to `reserved` so it can't be double-booked, then creates the `Reservation` with `status='reserved'` and `reserved_at=now()`
- **`POST /reservations/<uuid:pk>/cancel/`** — cancel a hold.
  - No body
  - Validation: 400 if the reservation's current `status` isn't `reserved` (e.g. already cancelled/expired/checked out)
  - Side effect (wrapped in one DB transaction): sets the reservation to `cancelled` and the associated `BookCopy.status` back to `available`

## Background Tasks

- **`expire_reservations`** (Celery, registered name `reservations.expire_reservations`)
  - Runs on a schedule: Celery Beat fires it every 15 minutes (`crontab(minute='*/15')`, defined in `core/settings.py` under `CELERY_BEAT_SCHEDULE` as `expire-reservations-every-15-minutes`)
  - Queries reservations with `status='reserved'` and `expires_at` in the past
  - For each one, in its own transaction: sets `Reservation.status = 'expired'`, then — only if the copy's status is still `reserved` (a defensive check, since a librarian could have manually marked it `lost`/`damaged` in the meantime) — sets `BookCopy.status = 'available'`
  - Each reservation is processed in its own atomic block, so one failure doesn't block or roll back the others; returns a short summary string as the task result

## Notes / Quirks

- The list and create endpoints share a URL (`/`) but use different serializers, switched via `get_serializer_class()` based on HTTP method — `ReservationListSerializer` is fully read-only, `ReservationCreateSerializer` handles validation/creation.
- Reservation creation and cancellation both mutate `BookCopy.status` as a side effect (not just `Reservation.status`) — the same is true of the `expire_reservations` task. Anything reading `BookCopy.status` elsewhere in the codebase is implicitly affected by this app.
- `tasks.py` imports `BookCopy` via `from book_copies.models import BookCopy` (no `apps.` prefix), while `views.py`/`serializers.py` use `from apps.book_copies.models import BookCopy` — worth double-checking if you ever see import errors around this task.
- Depends on `book_copies` (status constants `STATUS_AVAILABLE`/`STATUS_RESERVED`, mutated directly rather than via any API call) and `members` (FK target, plus search-by-name/email).
- No custom logic in `admin.py` (no models registered) or `apps.py` (standard `AppConfig`, label `reservations`).
