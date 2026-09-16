from rest_framework.pagination import PageNumberPagination


# ---------------------------------------------------------------------------
# INVENTORY PAGINATION
# ---------------------------------------------------------------------------
# PageNumberPagination adds `?page=` (and, because we set page_size_query_param
# below, `?page_size=`) support to a view automatically. DRF wraps the
# response in an object like:
#   {
#     "count": 532,
#     "next": "http://.../api/inventory/?page=3",
#     "previous": "http://.../api/inventory/?page=1",
#     "results": [ ...up to page_size rows... ]
#   }
# so API clients can page through large result sets instead of getting
# everything back in one giant response.
class InventoryPagination(PageNumberPagination):
    # Default number of rows returned per page when the client doesn't
    # specify `?page_size=`.
    page_size = 100

    # Lets a client request FEWER rows per page via `?page_size=20`, say,
    # for a lighter-weight mobile view.
    page_size_query_param = "page_size"

    # HARD CEILING: even if a client asks for `?page_size=5000`, DRF clamps
    # it down to this value. This is what actually enforces "each API call
    # returns up to 100 rows" — page_size alone only sets the *default*.
    max_page_size = 100