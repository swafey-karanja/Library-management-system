"""
core/settings.py — Central configuration for the entire Django project.

WHAT IS THIS FILE?
    Django reads this file at startup to configure everything: database
    connection, installed apps, middleware, authentication, static files, and
    more.Every setting here controls a specific behaviour of your backend.

DATA FLOW OVERVIEW:
    HTTP Request
        ↓
    Django WSGI/ASGI server (wsgi.py / asgi.py)
        ↓
    Middleware stack (security checks, auth token extraction, CORS headers.)
        ↓
    URL Router (core/urls.py) — matches the URL path to an app
        ↓
    App-level URL router (e.g. users/urls.py)
        ↓
    View / ViewSet (users/views.py) — your business logic
        ↓
    Serializer (users/serializers.py) — validates input / formats output
        ↓
    Model (users/models.py) — talks to PostgreSQL via Django ORM
        ↓
    HTTP Response (JSON)
"""

from pathlib import Path
from datetime import timedelta
import os
import sys
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# BASE DIRECTORY
# ---------------------------------------------------------------------------
# Path(__file__) is the absolute path to this settings.py file.
# .resolve().parent.parent goes two levels up to the project root
# (the folder that contains manage.py).
# We use BASE_DIR to build absolute paths to other files/folders.
BASE_DIR = Path(__file__).resolve().parent.parent

sys.path.insert(0, os.path.join(BASE_DIR, "apps"))
# ---------------------------------------------------------------------------
# ENVIRONMENT VARIABLES
# ---------------------------------------------------------------------------
# We use a .env file to store secrets (DB password, JWT secret, etc.)
# so they are NEVER hardcoded in source code.
# python-dotenv loads them into os.environ automatically.
# Create a file called '.env' in the same folder as manage.py.
load_dotenv(BASE_DIR / ".env")

# ---------------------------------------------------------------------------
# SECURITY
# ---------------------------------------------------------------------------
# SECRET_KEY is used by Django to sign cookies, sessions, and CSRF tokens.
# NEVER expose this in production. Pull it from the .env file.
SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "change-me-in-production")

# DEBUG = True enables detailed error pages and other dev helpers.
# MUST be False in production.
DEBUG = os.environ.get("DEBUG", "True") == "True"

# ALLOWED_HOSTS lists the domain names/IPs Django will serve.
# In development, localhost and 127.0.0.1 are enough.
# In production, add your actual domain, e.g. ["api.mylibrary.com"].
ALLOWED_HOSTS = os.environ.get(
    "ALLOWED_HOSTS",
    "localhost,127.0.0.1",
).split(",")

# ---------------------------------------------------------------------------
# INSTALLED APPS
# ---------------------------------------------------------------------------
# Django is built around "apps" — self-contained modules that each handle
# one concern. You register every app here so Django knows about it.
INSTALLED_APPS = [
    # --- Django built-in apps ---
    "django.contrib.admin",  # The /admin UI
    "django.contrib.auth",  # Built-in auth framework (users, permissions)
    "django.contrib.contenttypes",  # Generic FK support used by admin & permissions
    "django.contrib.sessions",  # Session engine
    "django.contrib.messages",  # One-time flash messages
    "django.contrib.staticfiles",  # Static file serving
    # --- Third-party packages ---
    "rest_framework",  # Django REST Framework — turns Django into an API server
    "rest_framework_simplejwt",  # JWT authentication support
    "rest_framework_simplejwt.token_blacklist",
    "corsheaders",  # Handles Cross-Origin Resource Sharing for the frontend
    # --- Our own apps (one per module) ---
    # Each app lives in its own folder inside the project root.
    "apps.users",  # User management (this is the first module we build)
]

# ---------------------------------------------------------------------------
# MIDDLEWARE
# ---------------------------------------------------------------------------
# Middleware are functions that wrap every request/response.
# They run in ORDER (top → bottom on request, bottom → top on response).
MIDDLEWARE = [
    "corsheaders.middleware.CorsMiddleware",  # Must be FIRST — adds CORS headers
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

# ---------------------------------------------------------------------------
# URL CONFIGURATION
# ---------------------------------------------------------------------------
# This tells Django where to find the root URL file.
# All URL routing starts in core/urls.py, which then delegates to each app.
ROOT_URLCONF = "core.urls"

# ---------------------------------------------------------------------------
# TEMPLATES
# ---------------------------------------------------------------------------
# We're building a pure API backend, so templates are only used by the
# Django admin panel. You can ignore this for API development.
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

# ---------------------------------------------------------------------------
# WSGI APPLICATION
# ---------------------------------------------------------------------------
# The WSGI application is the interface between Django and a web server
# (like Gunicorn or uWSGI) in production.
WSGI_APPLICATION = "core.wsgi.application"

# ---------------------------------------------------------------------------
# DATABASE — PostgreSQL
# ---------------------------------------------------------------------------
# Django supports multiple databases. We use PostgreSQL via psycopg2.
# All connection details are pulled from .env for security.
#
# YOUR .env FILE SHOULD CONTAIN:
#   DB_NAME=library_db
#   DB_USER=postgres
#   DB_PASSWORD=yourpassword
#   DB_HOST=localhost
#   DB_PORT=5432
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",  # Use the PostgreSQL backend
        "NAME": os.environ.get("DB_NAME", "library_db"),
        "USER": os.environ.get("DB_USER", "postgres"),
        "PASSWORD": os.environ.get("DB_PASSWORD", "admin"),
        "HOST": os.environ.get("DB_HOST", "localhost"),
        "PORT": os.environ.get("DB_PORT", "5432"),
    }
}

# ---------------------------------------------------------------------------
# PASSWORD VALIDATION
# ---------------------------------------------------------------------------
# Django enforces these password rules when creating/updating users.
AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": (
            "django.contrib.auth.password_validation."
            "UserAttributeSimilarityValidator"
        )
    },
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": ("django.contrib.auth.password_validation.CommonPasswordValidator")},
    {"NAME": ("django.contrib.auth.password_validation.NumericPasswordValidator")},
]

# ---------------------------------------------------------------------------
# INTERNATIONALISATION
# ---------------------------------------------------------------------------
LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"  # Store all datetimes in UTC; convert in the frontend
USE_I18N = True
USE_TZ = True  # Makes Django timezone-aware — important for due dates & fines!

# ---------------------------------------------------------------------------
# STATIC FILES
# ---------------------------------------------------------------------------
STATIC_URL = "static/"

# ---------------------------------------------------------------------------
# DEFAULT AUTO FIELD
# ---------------------------------------------------------------------------
# Django auto-creates a primary key for models that don't define one.
# We use UUIDs everywhere (matching your PostgreSQL schema), so this is a
# fallback for Django's own internal models (admin, auth, etc.).
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# ---------------------------------------------------------------------------
# DJANGO REST FRAMEWORK (DRF)
# ---------------------------------------------------------------------------
# DRF is the toolkit that turns Django into a REST API server.
# These settings apply globally to every API view.
REST_FRAMEWORK = {
    # Default authentication: JWT tokens sent in the Authorization header.
    # Format: "Authorization: Bearer <access_token>"
    "DEFAULT_AUTHENTICATION_CLASSES": (
        "rest_framework_simplejwt.authentication.JWTAuthentication",
    ),
    # By default, only authenticated users can access API endpoints.
    # Individual views can override this (e.g. login doesn't require auth).
    "DEFAULT_PERMISSION_CLASSES": ("rest_framework.permissions.IsAuthenticated",),
    # Return clean JSON error responses instead of HTML error pages.
    "EXCEPTION_HANDLER": "rest_framework.views.exception_handler",
}

# ---------------------------------------------------------------------------
# SIMPLE JWT — JSON Web Token Configuration
# ---------------------------------------------------------------------------
# JWT works like a signed passport:
#   1. User logs in → server issues ACCESS token (short-lived) + REFRESH token (longer-lived)
#   2. Frontend stores both tokens.
#   3. For every API call, frontend sends: "Authorization: Bearer <access_token>"
#   4. When the access token expires, frontend uses the refresh token to get a new one.
#   5. When the refresh token expires, the user must log in again.
SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(minutes=60),  # Access token valid for 1 hour
    "REFRESH_TOKEN_LIFETIME": timedelta(days=7),  # Refresh token valid for 7 days
    "ROTATE_REFRESH_TOKENS": True,  # Issue a new refresh token on every refresh call
    "BLACKLIST_AFTER_ROTATION": True,  # Invalidate the old refresh token after rotation
    "UPDATE_LAST_LOGIN": True,  # Update user's last_login field on each login
    "ALGORITHM": "HS256",  # Signing algorithm (HMAC-SHA256)
    "SIGNING_KEY": SECRET_KEY,  # Use our Django SECRET_KEY to sign tokens
    "AUTH_HEADER_TYPES": ("Bearer",),  # Expect "Authorization: Bearer <token>"
    "AUTH_HEADER_NAME": "HTTP_AUTHORIZATION",
    # What to include in the token payload.
    # We include user_id so every view can know who made the request.
    "USER_ID_FIELD": "user_id",
    "USER_ID_CLAIM": "user_id",
}

# ---------------------------------------------------------------------------
# CORS — Cross-Origin Resource Sharing
# ---------------------------------------------------------------------------
# Your React/Next.js frontend runs on a different port (e.g. localhost:3000)
# than the Django API (localhost:8000). Browsers block these cross-origin
# requests by default. CORS headers tell the browser it's safe to proceed.
CORS_ALLOWED_ORIGINS = os.environ.get(
    "CORS_ALLOWED_ORIGINS", "http://localhost:3000,http://127.0.0.1:3000"
).split(",")

# Allow the frontend to send cookies and Authorization headers cross-origin.
CORS_ALLOW_CREDENTIALS = True

# ---------------------------------------------------------------------------
# CUSTOM AUTH USER MODEL
# ---------------------------------------------------------------------------
# We tell Django to use OUR User model (in apps.users) instead of its
# built-in one. This is required because our users table uses UUID primary
# keys and has a library_id foreign key — things Django's default model
# doesn't have.
AUTH_USER_MODEL = "users.User"
