# Users App

## Purpose

This is the project's `AUTH_USER_MODEL` (`users.User`) — every other app's `request.user` is an instance of the model defined here. It's a custom `User` (UUID primary key, `library` FK scoping each user to one library) built on `AbstractBaseUser` instead of Django's default user, since it maps onto an existing PostgreSQL `users` table (`email` as the login field, no username). Beyond the user record itself, this app owns the full auth lifecycle: JWT login/logout, password change/reset, and email-based account activation (new users are created `inactive` until they confirm their email).

## Key Models (`models.py`)

### `User`
- **Fields**: `user_id` (UUID PK, auto-generated), `library` (FK, `on_delete=CASCADE`, DB column `library_id`), `name`, `email` (unique), `password` (DB column `password_hash` — always go through `set_password()`/`check_password()`, never touch it directly), `role` (choices `admin`/`librarian`/`user`, default `user`), `status` (choices `active`/`inactive`/`suspended`, default `active`), `created_at` (auto), `last_login` (nullable, present only because `AbstractBaseUser` expects it — not otherwise used).
- **`Meta`**: `managed = False`, `db_table = "users"` — this table already existed before Django; migrations never alter it. (The two token models below, by contrast, *are* Django-managed — their tables get created via normal migrations.)
- **Derived properties** (no DB column): `is_active` (`status == "active"`), `is_admin` (`role == "admin"`), `is_staff` (`role in [admin, librarian]` — lets librarians into the Django admin), `is_superuser` (`role == "admin"`). `has_perm()`/`has_module_perms()` both just return `is_superuser`.
- **Manager**: `UserManager.create_user(email, password, name, role, status, library_id, ...)` and `create_superuser(...)` (forces `role="admin"`, `status="active"`) — the latter is what `manage.py createsuperuser` calls.

### `PasswordResetToken` / `EmailActivationToken`
- Same shape: FK to `User` (`CASCADE`), `token_hash` (unique — SHA-256 hex digest of a random UUID4; the raw token only ever exists in the emailed link, never stored), `expires_at`, `is_used`, `created_at`. Both expose `is_valid()` → `not is_used and expires_at > now()`.
- Only difference is the expiry window: password reset = **1 hour**, email activation = **24 hours** (tables `password_reset_tokens` / `email_activation_tokens`).
- Side effect worth knowing: requesting a new token of either kind first marks any existing *unused* token for that user as `is_used=True` — so at most one live token per purpose per user at any time.

### `UserRole` / `UserStatus`
Plain constant classes (not models) holding the valid `role` / `status` choices — used to avoid magic strings.

## API Endpoints

**JWT tokens** (`core/urls.py`, not this app — `permission_classes` default to `AllowAny`):
- `POST /api/v1/auth/token/` — SimpleJWT `TokenObtainPairView`. Body `{email, password}` (`USERNAME_FIELD = "email"`). Authenticates via Django's standard `authenticate()`, which checks `user.is_active` — an inactive account is rejected. Returns `{access, refresh}` only (no user payload).
- `POST /api/v1/auth/token/refresh/` — `TokenRefreshView`. Body `{refresh}`. Returns a new `{access, refresh}` pair — `ROTATE_REFRESH_TOKENS` + `BLACKLIST_AFTER_ROTATION` are both on, so the old refresh token is blacklisted the moment it's used.
- Note: the frontend actually uses this app's own `/login/` below, not this endpoint directly — `/login/` wraps the same token issuance but also returns the serialized user. Both exist and both work; know that there are two paths to a token pair.

**This app** (`apps/users/urls.py`, mounted at `/api/v1/users/`):
- `POST login/` — `AllowAny`. Body `{email, password}`. Manually checks the user exists, the password matches, and `is_active`; returns `401` with the *same* generic "Invalid credentials" message for both "no such email" and "wrong password" (avoids leaking which), `403` if the account is inactive. On success: `{access, refresh, user}`.
- `POST logout/` — `IsAuthenticated`. Body `{refresh}`. Blacklists that refresh token.
- `GET me/` — `IsAuthenticated`. Returns the requester's own serialized profile.
- `POST activate/` — `AllowAny`. Body `{uid, token}`. Hashes the token, looks up the matching `EmailActivationToken`, checks `is_valid()`, then flips `status → active`. Calling it again on an already-active account returns a `200` "already activated" (idempotent) rather than an error.
- `POST password-reset/` — `AllowAny`. Body `{email}`. **Always** returns the same `200` generic message, whether the email exists, is inactive, or is invalid — deliberate anti-enumeration design. On a valid active user it creates a `PasswordResetToken` and emails the reset link.
- `POST password-reset/confirm/` — `AllowAny`. Body `{uid, token, new_password, confirm_password}`. `confirm_password` must equal `new_password` (serializer cross-field check); `new_password` runs through Django's `validate_password` (length/common-password/similarity rules). Marks the token used on success.
- `GET /` — `IsAdminOrLibrarian`. Optional query params `?name=` / `?email=` (tokenized, punctuation/order-insensitive match, combined with AND if both given — see `core/search.py`). Always scoped to `request.user.library_id` — even an admin only sees their own library's users here.
- `POST /` — `IsAdminOrLibrarian`. Body `{name, email, password, role, status, library_id?}`. `library_id` defaults to the requester's own library if omitted. Serializer rejects duplicate emails and **forces `status` to be `"inactive"`** — a user cannot be created already-active through this endpoint. An activation email is fired automatically after save.
- `GET /<user_id>/` — `IsSameUserOrAdminOrLibrarian` (object-level: same `user_id`, or role in `[admin, librarian]`).
- `PATCH /<user_id>/` — same permission class as `GET`. Body: any of `{name, role, status, library_id}` (partial). **Quirk to flag**: the permission check only asks "same user, or admin/librarian" — it does not additionally forbid a non-staff user from editing their *own* `role`/`status`/`library_id`, and `UserUpdateSerializer` doesn't special-case who's calling. In practice a regular user hitting their own `PATCH` endpoint can submit those fields; there's no separate field-level guard in the view.
- `DELETE /<user_id>/` — `IsAdminOrLibrarian`. Soft-deactivate only (`status → inactive`, row is kept — preserves history like borrows/fines). Blocks deactivating your own account.
- `POST /<user_id>/change-password/` — `IsSameUserOrAdmin` (same `user_id` **or** `role == admin` — note this one excludes librarian, unlike most other endpoints here). Body `{current_password, new_password}`. Verifies `current_password` against the stored hash, requires the new password differ from the current one, and runs `validate_password`.
- `POST /<user_id>/resend-activation/` — `IsAdminOrLibrarian`. No body. `400` if the account is already active; otherwise invalidates old activation tokens and sends a fresh one.

## Notes

- **`emails.py`** sends both the password-reset and account-activation emails by calling the **Resend** SDK directly (`resend.Emails.send`, keyed by `RESEND_API_KEY`/`RESEND_FROM_EMAIL`), with the HTML built inline as an f-string (no template engine). Both send functions swallow exceptions and just `print()` a log line — a failed send never surfaces as an API error to the caller. (Settings also configures a Django/Anymail `EMAIL_BACKEND` for Resend, but this app bypasses it and talks to the Resend SDK directly.)
- **Auth header**: `Authorization: Bearer <access_token>` (`SIMPLE_JWT["AUTH_HEADER_TYPES"] = ("Bearer",)`). Access tokens last 24h, refresh tokens 7 days, with rotation + blacklisting on every refresh.
- **Permission classes** (`core/permissions.py`): `IsAdminOrLibrarian`, `IsSameUserOrAdmin`, `IsSameUserOrAdminOrLibrarian` (an `IsAdminUser`-only class also exists but isn't used in this app). The `IsSameUser...` classes are object-level — they only take effect where a view explicitly calls `self.check_object_permissions(request, obj)`, which every relevant view here does.
- `AUTH_USER_MODEL = "users.User"` in settings — this app's `User` model is the one Django/DRF auth machinery uses project-wide.
