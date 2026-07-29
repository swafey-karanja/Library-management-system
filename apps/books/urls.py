from django.urls import path

from .views import (
    BookBulkUpdateView,
    BookCreateView,
    BookExportView,
    BookImportView,
    BookListView,
    BookStatsView,
    BookUpdateView,
)

# All paths here are relative to whatever prefix core/urls.py mounts
# this file under (e.g. 'api/books/').
#
# A NOTE ON ORDERING: Django checks these patterns top to bottom and
# uses the FIRST one that matches. Fixed-text paths like "stats/" or
# "advanced-search/" are placed ABOVE the "<uuid:book_id>/update/"
# pattern further down would only be a concern if a fixed path could
# also be mistaken for a UUID - it can't here, but it's a good habit to
# put specific/fixed paths before dynamic/parameterized ones so you
# never accidentally shadow a literal route with a catch-all pattern.
urlpatterns = [
    path("", BookListView.as_view(), name="list-books"),
    path("add/", BookCreateView.as_view(), name="create-book(s)"),
    path("stats/", BookStatsView.as_view(), name="book-stats"),
    path("export/", BookExportView.as_view(), name="export-books"),
    path("import/", BookImportView.as_view(), name="import-books"),
    path("bulk-update/", BookBulkUpdateView.as_view(), name="bulk-update-books"),
    path("<uuid:book_id>/update/", BookUpdateView.as_view(), name="update-book"),
]