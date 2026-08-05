"""
urls.py — book_copies app routes

Maps URL paths to views.py/statistics.py/import_export.py/bulk_create.py.
Kept per-app so each app owns its own routes.
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

# Namespaces these URLs, e.g. reverse('book_copies:list').
app_name = 'book_copies'

urlpatterns = [
    path('', BookCopyListView.as_view(), name='list'),

    # Create one or many copies — single object or array body, each
    # item optionally carrying `quantity`. See create.py.
    path('add/', BookCopyCreateView.as_view(), name='create'),

    # <uuid:pk> matches a UUID segment and passes it in as pk.
    path('<uuid:pk>/update/', BookCopyUpdateView.as_view(), name='update'),

    # Static path — doesn't collide with '<uuid:pk>/update/' above.
    path('bulk-update/', BookCopyBulkUpdateView.as_view(), name='bulk-update'),

    # Aggregate statistics
    path('stats/', BookCopyStatisticsView.as_view(), name='statistics'),

    # File download / upload — see import_export.py.
    path('export/', BookCopyExportView.as_view(), name='export'),
    path('import/', BookCopyImportView.as_view(), name='import'),
]

# --------------------------------------------------------------------
# Include this in the project-level urls.py:
#
#   from django.urls import path, include
#   urlpatterns = [
#       ...
#       path('api/book-copies/', include('book_copies.urls')),
#   ]
#
# Resulting endpoints:
#
#   GET  /api/book-copies/                          -> list (paginated)
#   GET  /api/book-copies/?page=2 / ?page_size=50
#   GET  /api/book-copies/?search=potter             -> single term
#   GET  /api/book-copies/?search=potter good        -> multiple terms (AND)
#   GET  /api/book-copies/?status=&condition=&library=&book_name=
#   GET  /api/book-copies/?ordering=acquired_at | -acquired_at | created_at | -created_at
#
#   POST /api/book-copies/add/
#     - single object body  -> creates one copy, single-object response
#         {"library": "<uuid>", "book": "<uuid>"}
#         {"library": "<uuid>", "book": "<uuid>", "barcode": "LIB-0001"}
#         {"library": "<uuid>", "book": "<uuid>", "quantity": 5}   (list response, >1 row)
#     - array body -> creates many, list response, e.g.
#         [
#             {"library": "<uuid>", "book": "<uuid_a>", "quantity": 5},
#             {"library": "<uuid>", "book": "<uuid_b>", "barcode": "LIB-0099"}
#         ]
#
#   PUT/PATCH /api/book-copies/<copy_uuid>/update/    -> update one copy
#   PATCH /api/book-copies/bulk-update/               -> update many (status/
#         condition/shelf_location/acquired_at only)
#
#   GET  /api/book-copies/stats/                 -> aggregate counts
#   GET  /api/book-copies/export/?format=csv|json      -> download (supports
#         the same search/filter/ordering params as the list endpoint)
#   POST /api/book-copies/import/                      -> CSV upload, upsert by barcode
