from django.urls import path
from .views import (
    UserListCreateView,
    UserDetailView,
    ChangePasswordView,
    LoginView,
    LogoutView,
    MeView,
    PasswordResetRequestView,
    PasswordResetConfirmView,
    EmailActivationView,
)

# All paths here are relative to the prefix set in core/urls.py → 'api/v1/users/'

urlpatterns = [
    # Auth
    path("login/", LoginView.as_view(), name="user-login"),
    path("logout/", LogoutView.as_view(), name="user-logout"),
    # Current user's own profile
    path("me/", MeView.as_view(), name="user-me"),
    # User management (admin / librarian)
    path("", UserListCreateView.as_view(), name="user-list-create"),
    path("<uuid:user_id>/", UserDetailView.as_view(), name="user-detail"),
    path(
        "<uuid:user_id>/change-password/",
        ChangePasswordView.as_view(),
        name="user-change-password",
    ),
    # Password reset (unauthenticated — for locked-out users)
    path(
        "password-reset/",
        PasswordResetRequestView.as_view(),
        name="password-reset-request",
    ),
    path(
        "password-reset/confirm/",
        PasswordResetConfirmView.as_view(),
        name="password-reset-confirm",
    ),
    path("activate/", EmailActivationView.as_view(), name="email-activation"),
]
