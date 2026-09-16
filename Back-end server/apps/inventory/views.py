from rest_framework import generics

from core.permissions import IsAdminOrLibrarian
from apps.users.models import UserRole

from .models import Inventory
from .serializers import InventoryListSerializer
from .pagination import InventoryPagination


# ---------------------------------------------------------------------------
# INVENTORY LIST VIEW
# GET /api/inventory/   → list inventory records, paginated (max 100/page)
#
# ACCESS RULES:
#   - Only 'admin' (super admin) and 'librarian' roles may call this at all.
#     Enforced by `permission_classes = [IsAdminOrLibrarian]`, the same
#     permission class already used for the equivalent user-management
#     endpoints — DRF calls its has_permission() before the view runs and
#     returns 403 Forbidden automatically if it fails.
#   - Admins see inventory rows across ALL libraries.
#   - Librarians only see inventory rows belonging to THEIR OWN library
#     (request.user.library_id). This is a query-level restriction, not
#     just a UI filter, so a librarian can never fetch another library's
#     data no matter what query params they send.
# ---------------------------------------------------------------------------
class InventoryListView(generics.ListAPIView):
    """
    generics.ListAPIView is a DRF shortcut class that already implements
    GET-list-with-pagination for you: it takes get_queryset(), runs it
    through pagination_class, serializes the page with serializer_class,
    and returns the response. We only need to supply those three pieces
    below instead of hand-writing the GET method ourselves (compare this
    to UserListCreateView in apps.users, which is a plain APIview because
    it also needs a custom POST).
    """

    serializer_class = InventoryListSerializer
    pagination_class = InventoryPagination
    permission_classes = [IsAdminOrLibrarian]

    def get_queryset(self):
        """
        get_queryset() runs on every request (not once at import time), so
        it's the right place to scope the results to the CURRENTLY
        authenticated user — `self.request.user` is only available once a
        real request comes in.
        """
        queryset = Inventory.objects.all()

        # Admins (super admins) can see inventory for every library.
        # Librarians are restricted to their own library only.
        if self.request.user.role == UserRole.LIBRARIAN:
            queryset = queryset.filter(library_id=self.request.user.library_id)

        # Explicit ordering matters for pagination: without it, PostgreSQL
        # doesn't guarantee row order between page requests, which can
        # cause rows to be skipped or repeated across pages as the
        # underlying table changes. Ordering by (library_id, book_id) gives
        # a stable, deterministic sequence.
        return queryset.order_by("library_id", "book_id")