# Notifications App

## Purpose
Handles all member email notifications for the library system: checkout confirmations, return confirmations, loan-update notices (when a librarian edits a transaction), plus scheduled "due soon" and "overdue" reminders. Unlike most other apps in this project, **it has no `urls.py`** — it's not exposed as a REST API. It's invoked internally by other apps (mainly `borrow_transactions`) and by Celery Beat on a schedule.

## Key Models
- **`NotificationLog`** — dedup/bookkeeping table only (not user-facing, no views/serializers). Fields:
  - `id` — UUID primary key.
  - `borrow_transaction_id` — plain `UUIDField` (indexed, **not** a FK) pointing at a `BorrowTransaction`. Kept as a raw UUID rather than a relation so this app doesn't need to import `borrow_transactions` models at load time.
  - `notification_type` — `"due_soon"` or `"overdue"` (choices).
  - `sent_for_date` — meaning differs by type: for `due_soon` it's the transaction's fixed `due_date` (so a loan gets reminded once, ever); for `overdue` it's *today's* date at scan time (so a loan can get reminded once per day it stays overdue).
  - `sent_at` — `auto_now_add` timestamp.
  - A `UniqueConstraint` on `(borrow_transaction_id, notification_type, sent_for_date)` enforces the dedup at the DB level, not just in code — a bulk insert that races against another scan run just silently no-ops via `ignore_conflicts=True`.
  - No custom methods beyond `__str__`.

## Background Tasks (Celery — `tasks.py`)
All tasks take only IDs/primitive values (never model instances) since Celery serializes args to JSON — each task re-fetches what it needs from the DB when it runs.

- **`send_checkout_email(transaction_ids)`** — `bind=True, max_retries=3, default_retry_delay=60`. Triggered by `borrow_transactions` (checkout views) via `transaction.on_commit(...).delay(...)`. Takes one or more transaction ids assumed to belong to the same member, fetches them, skips silently if the member has no email on file, renders `checkout.txt`/`.html`, and sends. Raises `self.retry()` on failure.
- **`send_return_email(transaction_ids)`** — same shape/retry behavior as checkout; renders `return.txt`/`.html` and includes each book's `fine_amount` plus a summed `total_fines`.
- **`send_update_email(transaction_id, changes)`** — one call per transaction (not batched, since a bulk update can touch different members at once). `changes` is a dict of `{field: {"old": ..., "new": ...}}` built by `borrow_transactions/notifications.py` for whichever of `due_date`, `returned_at`, `status`, `fine_amount` actually changed; values are pre-stringified/formatted there so they survive the JSON round-trip to the broker. Renders `transaction_update.txt`/`.html`; no-ops if `changes` is empty.
- **`send_due_soon_reminders()`** — no args, scheduled via `CELERY_BEAT_SCHEDULE` every 6h (`crontab(minute=0, hour="0,6,12,18")`). Finds active loans (`returned_at__isnull=True`) whose `due_date` falls in a rolling window centered ~24h out (`DUE_SOON_LEAD_HOURS=24`, window widened by `DUE_SOON_SCAN_INTERVAL_HOURS=6` on each side so a missed run is still caught by the next one), filters out ones already logged in `NotificationLog`, groups the rest by member (one email per member listing all their soon-due books), sends, then `bulk_create`s `NotificationLog` rows (`ignore_conflicts=True`). If a send fails, that transaction is deliberately **not** logged, so the next scan naturally retries it — no separate retry mechanism.
- **`send_overdue_reminders()`** — no args, scheduled daily at 07:00 (`crontab(minute=0, hour=7)`). Finds all active loans with `due_date` in the past. Side effect: recalculates every overdue transaction's `fine_amount` via `calculate_fine()` and persists it with `bulk_update` (previously `fine_amount` only became accurate at return time). Then dedupes against `NotificationLog` for today's date, groups by member, sends an "overdue" email per member (with `days_overdue`/`fine_amount` per book and a `total_fines`), and logs successes the same way as `send_due_soon_reminders`.

## Notable quirks / validation
- `_send_email()` is the shared helper: renders `templates/notifications/{base}.txt` and `{base}.html` and sends via `EmailMultiAlternatives` (HTML body attached as the alternative, plain text as the fallback for clients/spam filters that prefer it) — every task goes through this, so template pairs must stay in sync.
- Dedup is intentionally different in shape for the two reminder types (fixed due date vs. "today") — see `NotificationLog.sent_for_date` above; don't assume it always means "today".
- Templates live in `templates/notifications/`, one `.html`/`.txt` pair each: `checkout`, `return`, `transaction_update`, `due_soon`, `overdue`.
- Delivery goes through **`django-anymail`** configured for **Resend** (`RESEND_API_KEY`, `RESEND_FROM_EMAIL` in settings) — `_send_email` itself is backend-agnostic, it's the anymail config that routes it to Resend.

## Dependencies on other apps
- **`borrow_transactions`** is the sole integrator. Its views never call Celery/tasks directly — they go through `apps/borrow_transactions/notifications.py`, which wraps each call in `transaction.on_commit(lambda: send_x_email.delay(...))` so the email only fires after the DB transaction actually commits, and centralizes which fields are "email-worthy" (`NOTIFIABLE_FIELDS`) for update notices.
- **`reservations`** does not hook into this app at all — its own Celery task (`expire_reservations`, scheduled every 15 min) only flips reservation/book-copy status and never sends email.
