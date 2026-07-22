from rest_framework.permissions import BasePermission
from apps.users.models import UserRole

# ---------------------------------------------------------------------------
# BASE HELPER
# ---------------------------------------------------------------------------
# Centralises the authenticated-user check so every class below stays concise.


def _is_authenticated(request):
    return bool(request.user and request.user.is_authenticated)


# ---------------------------------------------------------------------------
# ROLE-BASED PERMISSION CLASSES
# ---------------------------------------------------------------------------
# These are used as `permission_classes` on views.
# DRF calls `has_permission()` before the view runs.
# Returning False sends a 403 Forbidden automatically.
#
# USAGE IN VIEWS:
#   permission_classes = [IsAdminUser]
#   permission_classes = [IsAdminOrLibrarian]
#   permission_classes = [IsAuthenticated, IsSameUserOrAdminOrLibrarian]


class IsAdminUser(BasePermission):
    """
    Grants access only to users with the 'admin' role.
    Used for: creating libraries, deleting books, and any other
    action that should never be available to librarians or staff.
    """

    message = "Admin role required."

    def has_permission(  # pyright: ignore[reportIncompatibleMethodOverride]
        self, request, view
    ) -> bool:
        return _is_authenticated(request) and request.user.role == UserRole.ADMIN


class IsAdminOrLibrarian(BasePermission):
    """
    Grants access to admins and librarians.
    Used for: listing users, editing members, managing copies/inventory,
    processing borrows, reservations, and fines.
    """

    message = "Admin or Librarian role required."

    def has_permission(  # pyright: ignore[reportIncompatibleMethodOverride]
        self, request, view
    ) -> bool:
        return _is_authenticated(request) and request.user.role in [
            UserRole.ADMIN,
            UserRole.LIBRARIAN,
        ]


# ---------------------------------------------------------------------------
# OBJECT-LEVEL PERMISSION CLASSES
# ---------------------------------------------------------------------------
# These are used alongside has_permission() for row-level checks.
# DRF calls `has_object_permission()` only after `has_permission()` passes,
# and only when the view explicitly calls `self.check_object_permissions(request, obj)`.


class IsSameUserOrAdmin(BasePermission):
    """
    Object-level: the requesting user can only act on their own record,
    unless they are an admin (who can act on any record).

    Used for: change-password endpoint — only the account owner or
    an admin should be able to change a given user's password.
    """

    message = "You can only modify your own account."

    def has_permission(  # pyright: ignore[reportIncompatibleMethodOverride]
        self, request, view
    ) -> bool:
        return _is_authenticated(request)

    def has_object_permission(self, request, view, obj):
        return (
            request.user.role == UserRole.ADMIN or request.user.user_id == obj.user_id
        )


class IsSameUserOrAdminOrLibrarian(BasePermission):
    """
    Object-level: the requesting user can act on their own record,
    or any admin/librarian can act on any record.

    Used for: retrieve and edit user endpoints — a staff user can
    view/edit their own profile; admins and librarians can view/edit anyone.
    """

    message = "You do not have permission to access this user record."

    def has_permission(  # pyright: ignore[reportIncompatibleMethodOverride]
        self, request, view
    ) -> bool:
        return _is_authenticated(request)

    def has_object_permission(self, request, view, obj):
        return (
            request.user.role in [UserRole.ADMIN, UserRole.LIBRARIAN]
            or request.user.user_id == obj.user_id
        )
