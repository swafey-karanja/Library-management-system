from django.urls import path
from .views import LibraryCreateView, LibraryListView, LibraryUpdateView

urlpatterns = [
    # End-point for listing all existing libraries
    path("", LibraryListView.as_view(), name="list-libraries"),
    # End-point for creating a new library entry
    path("create/", LibraryCreateView.as_view(), name="create-library"),
    # <library_id> is a URL parameter — Django captures whatever UUID is
    # in that position and passes it to the view as `library_id`, which
    # matches the lookup_url_kwarg we set on LibraryUpdateView.
    # e.g. PUT /api/libraries/123e4567-e89b-12d3-a456-426614174000/update/
    path(
        "<uuid:library_id>/update/", LibraryUpdateView.as_view(), name="update-library"
    ),
]
