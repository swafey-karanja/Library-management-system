# borrow_transactions

## Purpose

Models the checkout/return lifecycle of physical book copies: a member borrows a `BookCopy`, it's due back by a `due_date`, and it's either returned on time, returned late (incurring a fine), or marked lost. This is the app that tracks who has what, what's overdue, and what's owed — checkout and return are first-class actions here, not just field updates.

## Key Model: `BorrowTransaction`

- `id` — UUID primary key.
- `book_copy` (FK → `book_copies.BookCopy`), `member` (FK → `members.Member`) — both `on_delete=CASCADE`.
- `borrowed_at`, `due_date` — auto-filled by `save()` if left blank: `borrowed_at` = now, `due_date` = `borrowed_at` + `DEFAULT_LOAN_PERIOD_DAYS` (14).
- `returned_at` — null while still out.
- `status` — `active` / `returned` / `overdue` / `lost` (`STATUS_CHOICES`). Plain `CharField`, enforced by Django only, not a DB enum.
- `fine_amount` — `DecimalField`, default `0.00`.
- `created_at` — `auto_now_add`.
- **Business logic lives on the model**, not the views: `is_overdue` (property — true if unreturned and past `due_date`), `days_overdue` (property — whole days late vs. `returned_at` or now, never negative), `calculate_fine()` (method — `days_overdue * FINE_PER_DAY_LATE`, `$0.50/day`; doesn't save, just computes).
- `Meta`: `managed = False` (maps to an existing table, not Django-created), indexed on `book_copy`, `member`, `status`, and `(borrowed_at, due_date)`; default ordering `-borrowed_at`.
- **Known issue** (flagged in the model's docstring): the underlying SQL FK references `members(id)`, but `members`' actual PK column is `member_id` — same mismatch exists on `reservations` and `fines`. Not yet fixed at the DB level.

## API Endpoints

Unless noted otherwise, every endpoint below requires `IsAdminOrLibrarian` (`core.permissions`) and is scoped to the caller's library through the copy's library (`book_copy__library_id`) — a transaction belonging to another library is invisible/404, not just forbidden.

- **`GET /`** — List, paginated (`page_size` default 20, max 100 via `?page_size=`). Filters: `?status=`, `?member=<uuid>`, `?book_copy=<uuid>`. Search: `?search=` over member name / book title / barcode. Order: `?ordering=borrowed_at|-borrowed_at|due_date|-due_date|created_at`.
- **`GET /<uuid:pk>/`** — Single transaction.
- **`GET /active/<identifier>/`** — `<identifier>` is a `BookCopy` UUID *or* barcode. Returns that copy's current unreturned transaction; 404 if the copy doesn't exist or isn't checked out. Used by return-scan flows to get the transaction id needed for the return endpoint.
- **`POST /checkout/`** — Body: `{book_copy, member, borrowed_at?, due_date?}`. Rejects (409) if the copy isn't `available`, or if the checkout would push the member over their active-loan limit. Creates the transaction and flips the copy to `borrowed` atomically.
- **`POST /batch-checkout/`** — Body: `{member, book_copies: [uuid, ...], borrowed_at?, due_date?}`. All-or-nothing: any copy that's missing/unavailable, or a duplicate copy id, fails the whole batch (collected into an `errors` list, nothing written). Loan-limit check counts existing loans *plus* the whole batch together.
- **`POST /<uuid:pk>/return/`** — Body (all optional): `{returned_at?, condition?}`. 400 if already returned. Computes and stores `fine_amount`, sets copy status to `available` (or `damaged` if `condition` says so).
- **`POST /batch-return/`** — Body: `{returned_at?, transactions: [{id, condition?}, ...]}`. Same all-or-nothing behavior as batch-checkout; any missing/already-returned id fails the whole batch.
- **`PUT/PATCH /<uuid:pk>/update/`** — Librarian correction of one transaction. Accepts `book_copy`, `member`, `borrowed_at`, `due_date`, `returned_at`, `status`, `fine_amount` — this is the one place `fine_amount` is directly writable. Syncs the linked `BookCopy.status` to match the new transaction status, and emails the member if any of `due_date`/`returned_at`/`status`/`fine_amount` actually changed.
- **`PATCH /bulk-update/`** — Body: a *list* of `{id, status?, due_date?, returned_at?, fine_amount?}`. Deliberately excludes `book_copy`/`member` (that's a structural change, single-item only via the endpoint above). 400 on duplicate ids or any id not found/out of scope; otherwise all-or-nothing per request, with the same `BookCopy` sync + change-email as the single-item update.
- **`GET /export/?format=csv|json`** — Streams a file instead of JSON; reuses the same `status`/`member`/`book_copy`/`search`/`ordering` params as the list endpoint.
- **`GET /stats/`** — Aggregate counts only: totals by `status` (zero-filled for statuses with no rows), `currently_borrowed`, `currently_overdue` (computed in Python via `is_overdue`, not SQL, since it depends on "now").

## Serializer notes

- `BorrowTransactionSerializer` (list/detail/export) makes `fine_amount`/`id`/`created_at` read-only, and adds convenience fields (`member_name`, `book_title`, `barcode`, `is_overdue`, `days_overdue`) so clients don't need extra round-trips.
- `BorrowCheckoutSerializer.validate_book_copy` checks the copy is `available` — but this is only a first-pass check; the view re-fetches the copy with `select_for_update()` inside the atomic block as the actual race-safe gate.
- `BorrowBatchCheckoutSerializer` / `BorrowBatchReturnSerializer` reject an empty list and reject duplicate copy/transaction ids in the same request.
- `BorrowReturnSerializer.condition` and its batch equivalent reuse `BookCopy.CONDITION_CHOICES`, so copy condition stays in sync between apps.

## Side effects & dependencies

- **Notifications**: `notifications.py` is the only bridge to email — views never touch Celery directly. `notify_checkout` / `notify_return` fire from the checkout/return/batch endpoints; `notify_update` fires from the single and bulk update endpoints, but only for fields in `NOTIFIABLE_FIELDS` (`due_date`, `returned_at`, `status`, `fine_amount`) that actually changed. All are scheduled via `transaction.on_commit(...).delay(...)` into `apps.notifications.tasks`, so an email never fires for a write that gets rolled back.
- **`BookCopy` status sync**: checkout/return set the copy's status directly as part of the action; the update/bulk-update endpoints instead call a shared helper (`_sync_book_copy_status`) that maps transaction status → copy status (`active`/`overdue` → `borrowed`, `returned` → `available`, `lost` → `lost`).
- **Concurrency**: checkout, batch-checkout, and batch-return all use `select_for_update()` to lock the relevant `BookCopy`/`BorrowTransaction` rows before the authoritative availability check, so two simultaneous requests can't double-checkout the same copy or double-return the same loan. The active-loan-limit check is a plain count (not locked) — a deliberate best-effort tradeoff, called out in the code as lower stakes than double-lending a physical copy.
- **Active-loan limit**: per-library override via `library.enabled_modules["max_active_loans"]` (JSONB, no schema change needed); falls back to `DEFAULT_MAX_ACTIVE_LOANS_PER_MEMBER` (5) if unset.
- **Cross-app dependencies**: `book_copies` and `members` (via FK, and via `core.permissions` library-scoping through the copy), plus `notifications` (Celery email tasks). No signals are used — all side effects are explicit calls from the views.
