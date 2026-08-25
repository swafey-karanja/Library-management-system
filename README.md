# Library Management System

A library management system with a Django REST Framework backend and a
Next.js frontend, split into two top-level folders:

- **`Back-end server/`** — Django REST API: catalog, physical copies,
  members, borrowing/returns, reservations, notifications, and auth.
- **`Front-end UI/`** — Next.js web client that talks to the API above.

## Tech stack

**Backend:** Django 6 + Django REST Framework, PostgreSQL, JWT auth
(`djangorestframework_simplejwt`), Celery + Celery Beat for background/
scheduled jobs, Resend (via `django-anymail`) for outbound email, Redis
listed as the intended Celery broker.

**Frontend:** Next.js (TypeScript, App Router), Tailwind CSS, shadcn/ui,
TanStack Query, Axios.

## Prerequisites

- Python (see `Back-end server/.python-version`) and `pip`
- Node.js + npm (for the frontend)
- Docker Desktop (runs Postgres for local dev — see below)

## Running the backend

```bash
cd "Back-end server"

# 1. Install Python dependencies (first time, or after requirements.txt changes)
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt

# 2. Start Postgres (via Docker)
docker compose up -d

# 3. Apply migrations
python manage.py migrate

# 4. Run the API
python manage.py runserver
```

The API is now on `http://localhost:8000`, mounted under `/api/...` (see
`core/urls.py`) plus `/api/v1/auth/token/` and `/api/v1/auth/token/refresh/`
for JWT login/refresh.

**Background jobs (Celery):** the notifications app's due-soon/overdue
reminders and the reservations app's expiry task run as scheduled Celery
tasks (see `CELERY_BEAT_SCHEDULE` in `core/settings.py`). To run them:

```bash
celery -A core worker -l info
celery -A core beat -l info
```

⚠️ **Not yet fully wired up:** `core/settings.py` doesn't set
`CELERY_BROKER_URL` yet. The Redis broker itself already exists — it runs on
a separate machine, not this one — so wiring this up is just a matter of
pointing `CELERY_BROKER_URL` (and `CELERY_RESULT_BACKEND`, if used) at that
host's Redis URL, not standing up a new broker. The rest of the API works
fine without this — it only affects scheduled reminders/expiry.

## Running the frontend

```bash
cd "Front-end UI"
npm install   # first time, or after package.json changes
npm run dev
```

Runs on `http://localhost:3000` — already whitelisted in the backend's
`CORS_ALLOWED_ORIGINS`.

## Environment variables

`Back-end server/.env` (gitignored):

| Variable | Purpose |
|---|---|
| `DJANGO_SECRET_KEY` | Django signing key |
| `DEBUG` | Django debug mode |
| `DB_NAME` / `DB_USER` / `DB_PASSWORD` / `DB_HOST` / `DB_PORT` | Postgres connection (matches `docker-compose.yml` defaults) |
| `RESEND_API_KEY` / `RESEND_FROM_EMAIL` | Outbound email (password reset, notifications) |
| `CORS_ALLOWED_ORIGINS` | Origins allowed to call the API (defaults to the frontend's dev URL) |
| `FRONTEND_URL` | Used to build links in emails (e.g. password reset) |

`Front-end UI/.env.local` (gitignored):

| Variable | Purpose |
|---|---|
| `NEXT_PUBLIC_API_URL` | Base URL of the Django API (defaults to `http://localhost:8000`) |

## Backend apps

Each app under `Back-end server/apps/` has its own README with more detail:

- **`users`** — custom auth user model, JWT login, password reset/email activation.
- **`libraries`** — library "tenants"/branches the rest of the data is scoped to.
- **`members`** — library patrons (distinct from staff `users`).
- **`books`** — the catalog (titles/bibliographic records).
- **`book_copies`** — physical inventory of each book at a given library.
- **`borrow_transactions`** — checkout/return lifecycle, due dates, fines.
- **`reservations`** — members holding a copy that isn't currently available.
- **`notifications`** — email delivery (checkout/return/due-soon/overdue); no public API, triggered internally and by Celery Beat.

## Known gaps

- Celery broker (`CELERY_BROKER_URL`) not yet pointed at the (already-running, remote) Redis instance — see above.
- Docker Desktop must be installed manually before `docker compose up -d` will work (not scriptable in every environment).
