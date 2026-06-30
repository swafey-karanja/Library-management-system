from django.urls import path
from .views import LibraryCreateView, LibraryListView

urlpatterns = [
    # End-point for listing all existing libraries
    path("list/", LibraryListView.as_view(), name="list-libraries"),
    # End-point for creating a new library entry
    path("create/", LibraryCreateView.as_view(), name="create-library"),
]
