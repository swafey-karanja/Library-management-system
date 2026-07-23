from core.permissions import (
    IsAdminUser,
    # IsAdminOrLibrarian,
    #  IsSameUserOrAdmin,
    #  IsSameUserOrAdminOrLibrarian,
)
from .models import Book
from rest_framework import generics
from .serializers import BookSerializer


# Create your views here.
class BookListView(generics.ListAPIView):
    permission_classes = [IsAdminUser]

    """
        API endpoint for listing all existing Library records.
    
        generics.ListAPIView handles GET requests and:
            1. Fetches the queryset defined below (every Library row).
            2. Passes that queryset into the serializer with many=True
               (handled internally by DRF), which converts each Library
               instance into a JSON object.
            3. Wraps the result in a 200 OK response as a JSON array

    """

    queryset = Book.objects.all()

    serializer_class = BookSerializer


class BookCreateView(generics.CreateAPIView):
    permission_classes = [IsAdminUser]

    queryset = Book.objects.all()

    serializer_class = BookSerializer


class BookUpdateView(generics.UpdateAPIView):
    permission_classes = [IsAdminUser]

    queryset = Book.objects.all()

    serializer_class = BookSerializer

    # By default, DRF's UpdateAPIView looks up objects using `pk` as the
    # URL keyword argument. Our primary key column is `library_id`, so we
    # override both lookup_field (the model field to filter on) and
    # lookup_url_kwarg (the name of the URL parameter to read the value
    # from) to match.
    lookup_field = "book_id"
    lookup_url_kwarg = "book_id"
