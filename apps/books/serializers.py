from rest_framework import serializers
from .models import Book


class BookSerializer(serializers.ModelSerializer):
    """
    Serializer for the Book model.

    A ModelSerializer automatically generates serializer fields based on
    the model's fields, which saves us from redeclaring name/url/etc by
    hand. We still need to override behaviour for a couple of fields
    (book_id, created_at) and the create() method, explained below.
    """

    class Meta:
        # Tells the serializer which model to base itself on.
        model = Book

        # Explicitly list every field we want exposed through the API.
        # Being explicit (rather than using '__all__') is good practice —
        # it means if we add a sensitive field to the model later, it
        # won't accidentally get exposed through the API until we
        # deliberately add it here.
        fields = [
            "book_id",
            "title",
            "subtitle",
            "authors",
            "publication_year",
            "genre",
            "description",
            "isbn_13",
            "isbn_10",
            "cover_image",
            "created_at",
        ]

        # read_only_fields means: these fields will be INCLUDED when we
        # serialize a Library OUT to JSON (e.g. in the response after
        # creation), but the client is NOT allowed to set them when
        # sending data IN.
        read_only_fields = [
            "book_id",
            "created_at",
        ]
