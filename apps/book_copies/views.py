"""
views.py — BookCopy list view
================================

A "view" in DRF is the piece of code that actually handles an incoming
HTTP request and decides what to send back. Rather than writing the
GET-request logic (querying the DB, applying pagination, serializing
the result) by hand, DRF gives us `generics.ListAPIView` — a prebuilt
class that already knows how to do all of that for a simple
"list all objects" endpoint. We just tell it WHAT to list and HOW to
paginate/serialize it.
"""

from rest_framework import generics
from rest_framework.pagination import PageNumberPagination

from .models import BookCopy
from .serializers import BookCopySerializer


class BookCopyPagination(PageNumberPagination):
    """
    Controls how results are split into pages.

    PageNumberPagination expects a `?page=` query parameter, e.g.:
        GET /api/book-copies/?page=2

    - `page_size`: how many results are returned per page by default.
    - `page_size_query_param`: lets the CLIENT override the page size
      per-request, e.g. `?page_size=50`. This is optional but handy —
      remove this line if you'd rather enforce a fixed page size.
    - `max_page_size`: caps what a client can request via
      page_size_query_param, so nobody can request `?page_size=100000`
      and force the database to return everything at once.
    """
    page_size = 20
    page_size_query_param = 'page_size'
    max_page_size = 100


class BookCopyListView(generics.ListAPIView):
    """
    GET /api/book-copies/

    Returns a paginated list of all BookCopy rows.

    ListAPIView only supports the HTTP GET method (list), which is all
    we need for this module right now. If we later want to support
    creating a new BookCopy through this same endpoint, we'd switch to
    `generics.ListCreateAPIView` instead — it's a drop-in replacement
    that adds POST handling using the same serializer.
    """

    queryset = BookCopy.objects.select_related('library', 'book').all()

    # Tells the view which serializer to use to convert each BookCopy
    # instance into JSON.
    serializer_class = BookCopySerializer

    # Tells the view which pagination class to use for this endpoint
    # specifically. (You can also set a DEFAULT_PAGINATION_CLASS
    # project-wide in settings.py, but setting it here keeps this
    # view's behaviour explicit and self-contained.)
    pagination_class = BookCopyPagination

    # Results are unordered by default in Postgres unless we say
    # otherwise, which can make pagination inconsistent (the same row
    # could theoretically appear on two different pages, or be skipped,
    # if rows aren't returned in a stable order). Ordering by
    # `created_at` (newest first) keeps page results stable and
    # predictable across requests.
    def get_queryset(self):
        return super().get_queryset().order_by('-created_at')