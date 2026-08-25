# Members App

## Purpose

Manages **library patrons** — the people who borrow books — as distinct from
the `users` app, which handles staff/system accounts (admins and librarians)
that log in and use the API. A `Member` is *not* a `User`: it's a standalone
patron record scoped to one library, identified by a human-facing membership
number, with no login of its own and no FK to the `User` model. Staff users
authenticate separately and act on members on their library's behalf, using
`request.user.is_admin` / `request.user.library_id` for scoping.

## Key Models

- **Member** — a library patron record, mapped to a pre-existing `members`
  table (`managed = False`; no Django migrations create/alter it).
  - `member_id` — UUID primary key
  - `library` (FK to `libraries.Library`, `on_delete=CASCADE`) — every member belongs to exactly one library (multi-tenant isolation); deleting a library deletes its members
  - `membership_no` — unique, human-facing library-card ID, server-generated (not user-settable)
  - `name`, `email`, `phone_number` — required; `address` — optional
  - `status` — `active` / `inactive` (choices defined on a plain `Status` class, not a model)
  - `gender` — `male` / `female` (default `female`)
  - `membership_type` — `student` / `normal` (default `normal`)
  - `created_at` — auto-set on creation
  - No custom model methods beyond `__str__` (`"name (membership_no)"`) — business logic (membership-number generation, etc.) lives in the serializer, not the model.

## API Endpoints

All endpoints require the **`IsAdminOrLibrarian`** permission (from
`core.permissions`) — there's no per-endpoint variation. Routes are mounted
under the app's URL prefix (e.g. `/members/`):

- **`GET /`** — list members, paginated 30/page.
  - `?search=<term>` — quick single-term match across name, membership_no, email, phone_number
  - `?name=`, `?email=`, `?membership_no=` — tokenized, punctuation/order-insensitive match (combinable, AND'd)
  - `?phone_number=` — digits-only match, ignores formatting
  - `?membership_type=`, `?gender=`, `?status=` — exact match
  - `?created_at_after=`, `?created_at_before=` — date range (ISO date, either optional)
  - `?ordering=name` / `?ordering=-created_at` — whitelisted to `name`, `membership_no`, `created_at`
  - Defaults to `status=active` only, unless `status` is explicitly passed
- **`POST /add/`** — create a member. Body: `library`, `name`, `email`, `phone_number`, `gender`, `membership_type`, `status`, optional `address`. `membership_no`/`member_id`/`created_at` are server-set and rejected if sent.
- **`PUT`/`PATCH /<member_id>/update/`** — update a member; same writable fields as create. PATCH for partial updates (typical "edit member" form use).
- **`PATCH /bulk-update/`** — body: `{"member_ids": [...], "status"?, "gender"?, "membership_type"?}` — at least one of the three required; other values rejected with 400. `member_ids` outside the caller's library are silently dropped (not errored), to avoid leaking cross-library existence.
- **`GET /stats/`** — no params; returns `total_members` plus counts `by_status`, `by_gender`, `by_membership_type`, scoped like the list view.
- **`GET /export/`** — same search/filter/ordering params as the list view, but returns a full (unpaginated) CSV download of all matching rows.
- **`POST /import/`** — multipart form with `file` (CSV, header row required, `name` at minimum; optional `email`/`phone_number`/`address`/`gender`/`membership_type`) and `library_id` (required if caller is admin; ignored/overridden to the caller's own library otherwise). Any `membership_no` column in the file is ignored. Each row is validated independently — a bad row is reported in the response, not fatal to the rest of the file (207 Multi-Status if any errors).

## Notes

- **Library isolation**: every view scopes queries via `scoped_members_for(user)` in `views.py` — admins see all members, everyone else only their own library's. Reused across list/stats/export/bulk-update so isolation can't drift out of sync.
- **`membership_no` generation** (in `serializers.py`): format `<LIB_CODE>-NNNNNN`, numbered per library starting at 1. Generated inside a transaction with up to 5 retries on `IntegrityError` (handles two members being created for the same library concurrently). If all retries collide, the request fails with a validation error rather than silently duplicating.
- **Validation**: `email`/`phone_number` are required (non-blank, non-null) at the model level; `address` is optional. `member_id`, `created_at`, and `membership_no` are read-only on the serializer, so they can't be set/changed via the API even if included in a request body.
- No signals or other apps' code are triggered by member create/update/delete — effects are confined to this app's own views/serializer.
- **Cross-app dependencies**: FK to `libraries.Library`; permission class from `core.permissions`; relies on the authenticated `request.user` (from the `users` app) exposing `is_admin` and `library_id` for all the scoping/isolation logic above.
