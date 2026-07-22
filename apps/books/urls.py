from django.urls import path
from .views import BookListView

urlpatterns = [
    path("list/", BookListView.as_view(), name="list-books"),
    # path("add/", BooksAddView.as_view(), name="add-book(s)"),
    # path("<uuid:book_id>/update/", BookUpdateView.as_view(), name="update-book"),
]
