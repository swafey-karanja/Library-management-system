from core.permissions import (
    # IsAdminUser,
    IsAdminOrLibrarian,
    #  IsSameUserOrAdmin,
    #  IsSameUserOrAdminOrLibrarian,
)
from .models import Book
from rest_framework import generics
from .serializers import BookSerializer


# Create your views here.
class BookListView(generics.ListAPIView):
    permission_classes = [IsAdminOrLibrarian]

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
