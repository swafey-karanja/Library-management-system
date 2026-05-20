"""
core/urls.py — Root URL Configuration (the "main router").

WHAT IS THIS FILE?
    Every HTTP request that reaches Django first lands here.
    Django reads the `urlpatterns` list from top to bottom and tries to
    match the request path against each pattern.
    When a match is found, Django forwards the request to the corresponding
    view or sub-router (include).

HOW URL ROUTING WORKS (data flow):
    Request: POST /api/v1/users/login/
        ↓
    core/urls.py   → matches 'api/v1/users/' → hands off to apps.users.urls
        ↓
    apps/users/urls.py → matches 'login/' → calls LoginView
        ↓
    LoginView.post() → validates credentials → returns JWT tokens

VERSIONING STRATEGY:
    We prefix every API route with /api/v1/ so that in future, if we
    introduce breaking changes, we can add /api/v2/ without removing v1.
    Clients on v1 keep working while we migrate them gradually.
"""

# from django.contrib import admin
from django.urls import path, include

# ---------------------------------------------------------------------------
# JWT token endpoints
# ---------------------------------------------------------------------------
# SimpleJWT provides two ready-made views:
#   TokenObtainPairView  → POST /api/v1/auth/token/       (login — returns access + refresh)
#   TokenRefreshView     → POST /api/v1/auth/token/refresh/ (get a new access token)
# We import them here and wire them into our URL tree.
from rest_framework_simplejwt.views import (
    TokenObtainPairView,
    TokenRefreshView,
)

urlpatterns = [
    # -----------------------------------------------------------------------
    # Django Admin Panel
    # -----------------------------------------------------------------------
    # The built-in admin UI lives at /admin/.
    # Useful for quick data inspection during development.
    # -----------------------------------------------------------------------
    # JWT Authentication endpoints
    # -----------------------------------------------------------------------
    # POST /api/v1/auth/token/
    #   Body: { "email": "...", "password": "..." }
    #   Returns: { "access": "<token>", "refresh": "<token>" }
    path(
        "api/v1/auth/token/",
        TokenObtainPairView.as_view(),
        name="token_obtain_pair",
    ),
    # POST /api/v1/auth/token/refresh/
    #   Body: { "refresh": "<refresh_token>" }
    #   Returns: { "access": "<new_access_token>", "refresh": "<new_refresh_token>" }
    path(
        "api/v1/auth/token/refresh/",
        TokenRefreshView.as_view(),
        name="token_refresh",
    ),
    # -----------------------------------------------------------------------
    # Users module
    # -----------------------------------------------------------------------
    # All URLs starting with 'api/v1/users/' are handled by the users app.
    # include() delegates routing to apps/users/urls.py.
    # This keeps each module's routes contained inside its own app — clean
    # and easy to maintain as the project grows.
    path("api/v1/users/", include("apps.users.urls")),
]
