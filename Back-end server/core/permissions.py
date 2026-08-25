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

"""
core/permissions.py — LIBRARY SCOPING ADDITIONS

THREE PIECES, THREE DIFFERENT JOBS:

  1. scope_queryset_to_library()  — THE GLOBAL SCOPE FUNCTION. Filters
     a queryset (many rows) down to one library. Call it inside
     get_queryset(), or directly wherever a plain APIView builds its
     own queryset (checkout, return, bulk/batch endpoints, etc.).

  2. LibraryScopedQuerysetMixin    — a mixin for DRF generic views
     (ListAPIView, RetrieveAPIView, UpdateAPIView, GenericAPIView)
     that calls piece #1 automatically, by overriding get_queryset()
     ONCE. Inherit this instead of hand-filtering every such view.

  3. IsSameLibrary                 — an object-level permission class
     for plain APIView endpoints that fetch a single object by hand
     (checkout, return, etc.) — call
     `self.check_object_permissions(request, obj)` after fetching it.

WHY THREE PIECES INSTEAD OF ONE:
DRF's permission system only ever checks ONE object at a time
(has_object_permission), or the request before any object exists
(has_permission) — there's no hook that runs once per row of a list.
So a permission class alone can NEVER filter a list/export/statistics
endpoint; only a scoped queryset can. That's why #1/#2 (queryset-based)
and #3 (permission-based) both exist — they cover different endpoint
shapes, not the same one twice.
"""

from rest_framework.permissions import BasePermission


def get_user_library_id(user):
    """
    The library a non-admin user belongs to; None for an admin user
    (meaning "don't filter — show everything"), and also None
    (defensively) if `user` is missing or unauthenticated.
    """
    if user is None or not getattr(user, "is_authenticated", False):
        return None
    if getattr(user, "is_admin", False):
        return None
    return getattr(user, "library_id", None)


def scope_queryset_to_library(queryset, user, library_lookup="library_id"):
    """
    THE GLOBAL SCOPE FUNCTION.

    Filters `queryset` to rows belonging to `user`'s library. Returns
    `queryset` completely untouched for an admin user (or if the user
    has no library_id for some reason — fails open to "everything"
    ONLY for admins, and fails CLOSED to "nothing" is NOT done here;
    see the note below on why).

    `library_lookup` is the ORM path from THIS queryset's model down
    to a library id — plain field name for a direct FK, or Django's
    double-underscore syntax to reach through a relation for models
    with no direct library FK of their own:

        users                -> 'library_id'             (direct FK)
        book_copies           -> 'library_id'             (direct FK)
        members                -> 'library_id'             (direct FK, assumed)
        reservations            -> 'library_id'             (direct FK)
        fines                    -> 'library_id'             (direct FK)
        borrow_transactions       -> 'book_copy__library_id'
            (no direct library FK on this table — scoped via the
            copy's library instead, since the physical copy belongs to
            exactly one library, and that's the library actually
            lending it out)

    NOTE on non-admin users with no library_id: this shouldn't happen
    in practice (users.library_id is NOT NULL in the schema), but if
    it ever did, get_user_library_id() returns None for them too —
    same as an admin — which would incorrectly show them everything.
    If that's a real concern, add an explicit check for
    `user.is_authenticated and not user.is_admin and user.library_id
    is None` wherever this is called and reject the request outright,
    rather than relying on this function alone to catch it.

    Usage inside a generic view (or via LibraryScopedQuerysetMixin,
    which does exactly this for you):
        def get_queryset(self):
            queryset = super().get_queryset()
            return scope_queryset_to_library(queryset, self.request.user)

    Usage inside a plain APIView with its own queryset:
        book_copies = scope_queryset_to_library(
            BookCopy.objects.all(), request.user
        )
    """
    user_library_id = get_user_library_id(user)
    if user_library_id is None:
        return queryset
    return queryset.filter(**{library_lookup: user_library_id})


class LibraryScopedQuerysetMixin:
    """
    Mix into any DRF generic view (ListAPIView, RetrieveAPIView,
    UpdateAPIView, GenericAPIView) to have its queryset automatically
    scoped to the requesting user's library. Admins see everything.

    Set `library_lookup` on the view ONLY if the model doesn't have a
    direct `library_id` column (e.g. BorrowTransaction) — otherwise
    the default is correct and nothing extra is needed.

    Example:
        class BookCopyListView(LibraryScopedQuerysetMixin, generics.ListAPIView):
            queryset = BookCopy.objects.select_related('library', 'book').all()
            serializer_class = BookCopySerializer
            # library_lookup not set -> defaults to 'library_id', correct here

        class BorrowTransactionListView(LibraryScopedQuerysetMixin, generics.ListAPIView):
            queryset = BorrowTransaction.objects.select_related(...).all()
            serializer_class = BorrowTransactionSerializer
            library_lookup = 'book_copy__library_id'  # no direct FK on this model

    Why this belongs on get_queryset() specifically: RetrieveAPIView /
    UpdateAPIView's built-in get_object() calls get_queryset() first,
    then fetches by pk from WITHIN that queryset. If a row isn't in
    the scoped queryset, get_object() raises a plain 404 — not a 403
    from a permission class. That's the better behavior here: a 404
    tells the caller "this doesn't exist," rather than confirming
    "this exists, but isn't yours," which would leak that another
    library's UUID is valid.
    """

    library_lookup = "library_id"

    def get_queryset(self):
        queryset = super().get_queryset()
        return scope_queryset_to_library(queryset, self.request.user, self.library_lookup)


def get_object_library_id(obj):
    """
    Best-effort "which library does this object belong to", for
    IsSameLibrary's per-object check below (used by plain APIViews
    that fetch one object by hand rather than through a scoped
    queryset). Tries a direct library_id first, then falls back to
    known relations for models without one (e.g. BorrowTransaction).
    Extend this if another model needs a different path.
    """
    if hasattr(obj, "library_id"):
        return obj.library_id
    if getattr(obj, "book_copy", None) is not None:
        return obj.book_copy.library_id
    if getattr(obj, "member", None) is not None:
        return obj.member.library_id
    return None


class IsSameLibrary(BasePermission):
    """
    Object-level permission for plain APIView endpoints that fetch a
    single object by hand (checkout, return, etc. — anywhere there's
    no get_queryset() for LibraryScopedQuerysetMixin to hook into).
    Admins bypass this entirely.

    Usage — call check_object_permissions yourself once you have the
    object, same pattern as your existing UserDetailView already uses
    for IsSameUserOrAdminOrLibrarian:

        class BorrowReturnView(APIView):
            permission_classes = [IsSameLibrary]

            def post(self, request, pk):
                borrow_transaction = get_object_or_404(BorrowTransaction, pk=pk)
                self.check_object_permissions(request, borrow_transaction)
                ...

    For endpoints that touch MULTIPLE objects (batch-checkout,
    batch-return, bulk-update), call check_object_permissions once per
    object in the loop, before writing anything — same all-or-nothing
    principle as those endpoints already use for other validation.
    """

    message = "You do not have access to this library's data."

    def has_permission(self, request, view):
        # Only gates "is this an authenticated request at all" here.
        # The real check happens once an object exists, below.
        return bool(request.user and request.user.is_authenticated)

    def has_object_permission(self, request, view, obj):
        if getattr(request.user, "is_admin", False):
            return True

        user_library_id = get_user_library_id(request.user)
        object_library_id = get_object_library_id(obj)

        # Fail closed if either side couldn't be resolved.
        if user_library_id is None or object_library_id is None:
            return False

        return user_library_id == object_library_id