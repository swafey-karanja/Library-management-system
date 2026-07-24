from django.urls import path
from .views import BookListView, BookCreateView, BookUpdateView

urlpatterns = [
    path("", BookListView.as_view(), name="list-books"),
    path("add/", BookCreateView.as_view(), name="create-book(s)"),
    path("<uuid:book_id>/update/", BookUpdateView.as_view(), name="update-book"),
]
