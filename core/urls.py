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

from django.contrib import admin
from django.urls import path, include
from rest_framework_simplejwt.views import (
    TokenObtainPairView,
    TokenRefreshView,
)

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/v1/auth/token/", TokenObtainPairView.as_view(), name="token_obtain_pair"),
    path(
        "api/v1/auth/token/refresh/", TokenRefreshView.as_view(), name="token_refresh"
    ),
    path("api/v1/users/", include("apps.users.urls")),
]
