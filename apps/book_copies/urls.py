"""
urls.py — book_copies app routes
===================================

This file maps URL paths to the views defined in views.py, scoped to
this specific app. Keeping a urls.py PER APP (rather than dumping every
route into the project's main urls.py) keeps things modular — each
app owns its own routes, and the project just "includes" them.
"""

from django.urls import path

from .views import (
    BookCopyListView,
    BookCopyCreateView,
    BookCopyUpdateView,
    BookCopyBulkUpdateView,
    BookCopyStatisticsView,
    BookCopyExportView,
    BookCopyImportView
)

# `app_name` sets a "namespace" for these URLs. This matters once your
# project has many apps that might reuse the same view name (e.g. both
# `books` and `book_copies` might have a view called "list") — with a
# namespace, you can always refer to this one unambiguously elsewhere
# in Django as 'book_copies:list'.
app_name = 'book_copies'

urlpatterns = [
    # path(route, view, name):
    #   - route: the URL segment, relative to wherever this file gets
    #     `include()`-d in the project's main urls.py. An empty string
    #     '' means "the base path itself" (see project urls.py note
    #     below for the full resulting path).
    #   - view: `.as_view()` converts our class-based view into a
    #     plain function Django's URL dispatcher can call — all
    #     class-based views need this.
    #   - name: an internal identifier used to refer to this URL
    #     elsewhere in Django code (e.g. `reverse('book_copies:list')`)
    #     instead of hardcoding the path string everywhere.
    path('', BookCopyListView.as_view(), name='list-book-copies'),

    # POST-only endpoint for adding a new book copy.
    path('add/', BookCopyCreateView.as_view(), name='create-book-copies'),

    # `<uuid:pk>` is a Django URL "path converter" — it matches a
    # UUID-formatted segment of the URL (e.g.
    # 550e8400-e29b-41d4-a716-446655440000) and passes it into the
    # view as the keyword argument `pk`, which BookCopyUpdateView then
    # uses (via `lookup_field = 'pk'`) to find the matching BookCopy
    # row. Using the `uuid` converter (rather than plain `str`) means
    # Django rejects non-UUID-shaped URLs before the request even
    # reaches our view.
    path('<uuid:pk>/update/', BookCopyUpdateView.as_view(), name='update-book-copies'),

    # Bulk-update endpoint — accepts a list of {id, ...fields} objects
    # in the request body rather than a single id in the URL. Note
    # this is a STATIC path ('bulk-update/'), not a dynamic one, so it
    # doesn't collide with '<uuid:pk>/update/' above — Django matches
    # URL patterns top-to-bottom and 'bulk-update/' will never
    # accidentally be interpreted as a UUID by the pattern above it,
    # but as a general habit, static paths are still worth placing
    # before dynamic ones with a similar shape to avoid ambiguity.
    path('bulk-update/', BookCopyBulkUpdateView.as_view(), name='bulk-update-book-copies'),

    # Read-only aggregate statistics — total counts, breakdowns by
    # status/condition/library, etc. See statistics.py for the logic.
    path('statistics/', BookCopyStatisticsView.as_view(), name='statistics'),

    # Download the current (optionally filtered) set of book copies as
    # a CSV or JSON file. See import_export.py for the logic.
    path('export/', BookCopyExportView.as_view(), name='export'),

    # Upload a CSV file to bulk create/update book copies. See
    # import_export.py for the logic.
    path('import/', BookCopyImportView.as_view(), name='import'),
]

# --------------------------------------------------------------------
# HOOKING THIS UP TO THE PROJECT
# --------------------------------------------------------------------
# In your project-level urls.py (the one next to settings.py), include
# this file like so:
#
#   from django.urls import path, include
#
#   urlpatterns = [
#       ...
#       path('api/book-copies/', include('book_copies.urls')),
#   ]
#
# With the route above being '', the resulting URLs become:
#
#   GET  /api/book-copies/                     -> list (page 1)
#   GET  /api/book-copies/?page=2               -> page 2
#   GET  /api/book-copies/?page_size=50         -> 50 results per page
#
#   GET  /api/book-copies/?search=potter         -> single search term
#   GET  /api/book-copies/?search=potter good    -> multiple terms (AND)
#
#   GET  /api/book-copies/?status=available       -> filter by status
#   GET  /api/book-copies/?condition=new           -> filter by condition
#   GET  /api/book-copies/?library=<library_uuid>  -> filter by library
#   GET  /api/book-copies/?book_name=potter        -> filter by book title
#   GET  /api/book-copies/?status=available&condition=new&book_name=potter
#                                                    -> filters combine (AND)
#
#   GET  /api/book-copies/?ordering=acquired_at    -> oldest acquired first
#   GET  /api/book-copies/?ordering=-acquired_at   -> newest acquired first
#   GET  /api/book-copies/?ordering=created_at     -> oldest created first
#   GET  /api/book-copies/?ordering=-created_at    -> newest created first (default)
#
#   All of the above can be combined in a single request, e.g.:
#   GET  /api/book-copies/?search=potter&status=available&ordering=-acquired_at&page=2
#
#   POST /api/book-copies/create/                  -> add a new copy
#
#   PUT   /api/book-copies/<copy_uuid>/update/      -> full update
#   PATCH /api/book-copies/<copy_uuid>/update/      -> partial update
#
#   PATCH /api/book-copies/bulk-update/             -> bulk update many
#         copies at once. Body is a JSON array; each item may only
#         contain: id (required) plus any of status, condition,
#         shelf_location, acquired_at. e.g.
#         [
#             {"id": "<uuid-1>", "status": "borrowed"},
#             {"id": "<uuid-2>", "condition": "good", "shelf_location": "B2-04"}
#         ]

#   GET  /api/book-copies/statistics/               -> aggregate counts
#         (total, by status, by condition, by library, recently
#         acquired, missing shelf location)
#
#   GET  /api/book-copies/export/?format=csv         -> download as CSV
#   GET  /api/book-copies/export/?format=json        -> download as JSON
#         Accepts the SAME search/filter/ordering query params as the
#         list endpoint above, so exports can be scoped, e.g.:
#         GET /api/book-copies/export/?status=available&format=csv
#
#   POST /api/book-copies/import/                    -> bulk import from
#         an uploaded CSV file (multipart/form-data, field name "file").
#         Required columns: library, book, barcode. Optional columns:
#         status, condition, shelf_location, acquired_at. Existing rows
#         are matched (and updated) by barcode; unmatched barcodes
#         create new rows.