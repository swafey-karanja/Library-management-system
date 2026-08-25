# Libraries App

## Purpose

Manages `Library` records — the individual library **tenants** in this system (e.g. "Greenfield Library", "Riverside Library"). Each `Library` represents one organization/branch using the platform, with its own branding, enabled feature modules, and status. Other apps scope their data to a specific library (e.g. the `User` model has a `library_id` foreign key, per the project setup notes), making this a foundational, multi-tenancy app that most of the rest of the system depends on — deactivating or misconfiguring a `Library` here has knock-on effects everywhere else.

The `library` table is created and managed directly in PostgreSQL (via raw SQL), not through Django migrations — the model uses `managed = False` (`db_table = "library"`) and any schema changes must be made in Postgres directly, not via `makemigrations`.

## Key Model

**`Library`** — a single library tenant.

- `library_id` (UUID, PK) — generated on the Python side via `uuid.uuid4()` at instance-creation time, not by a DB default. Because `managed = False`, Django can't rely on Postgres's `gen_random_uuid()` default being triggered through the ORM, so the serializer's `create()` explicitly sets this.
- `name` (unique, required) — display name; DB has a supporting index (`idx_library_name`).
- `url` (unique, optional) — public-facing subdomain/URL.
- `logo`, `primary_color`, `secondary_color` — branding fields (all optional strings, e.g. hex codes/paths).
- `enabled_modules` (JSONB, default `{}`) — a JSON *object* describing which feature modules are on for this library, e.g. `{"members": true, "fines": false}`. Enforced as a dict (not list/string/number) by serializer validation.
- `status` — `Library.Status` choices: `"active"` / `"inactive"`, defaults to `"active"`. Used to gate whether a tenant is currently live.
- `created_at` — auto-set on creation (`auto_now_add`), read-only.
- No custom model methods/properties beyond `__str__` (returns `name`); no signals are defined in this app.

## API Endpoints

All three views require `IsAdminUser` permission (from `core.permissions`) — there's no per-endpoint variation.

- **`GET /`** — `LibraryListView`. Lists all libraries, ordered by `name`.
  - Query param: `?name=<query>` — tokenized, punctuation/order-insensitive search via `core.search.filter_by_tokens` (same matching used for books/members/users), e.g. `?name=Green Library` and `?name=Library, Green` both match "Greenfield Library". Omit to get every library.
- **`POST /add/`** — `LibraryCreateView`. Creates a new library.
  - Body: `name` (required), plus optionally `logo`, `url`, `primary_color`, `secondary_color`, `enabled_modules`, `status`. `library_id` and `created_at` are server-generated and rejected/ignored if sent.
  - Returns 201 with the full serialized object, or 400 with field-level errors.
- **`PUT`/`PATCH /<uuid:library_id>/update/`** — `LibraryUpdateView`. Updates an existing library, looked up by `library_id` (`lookup_field`/`lookup_url_kwarg` both set to `library_id`, not DRF's default `pk`).
  - PUT expects all editable fields (missing ones fail validation); PATCH sends only the fields to change — the serializer's custom `update()` uses `validated_data.get(field, instance.field)` per field to support this, rather than relying on `ModelSerializer`'s default update.
  - 404 if the UUID doesn't match any row.

## Validation & Serializer Notes

- `LibrarySerializer` explicitly lists its `fields` (not `__all__`) so newly added model fields aren't auto-exposed through the API.
- `validate_name` strips whitespace and rejects empty/whitespace-only names (the DB only enforces `NOT NULL`, not "non-blank").
- `validate_status` re-checks the value against `Library.Status.choices` to return a clearer error message than DRF's default.
- `validate_enabled_modules` requires a JSON object/dict if provided (a JSONB column technically allows any JSON value, but this app constrains it to a mapping).

## Dependencies

- **Depended on by**: other apps' models that scope data per-tenant via a `library_id` FK (e.g. `User`) — this app is effectively the root of the system's multi-tenancy model.
- **Depends on**: `core.permissions.IsAdminUser` (all endpoints are admin-only) and `core.search.filter_by_tokens` (powers the `?name=` search).
