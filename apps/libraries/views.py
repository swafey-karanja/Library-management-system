from rest_framework import generics
from .models import Library
from .serializers import LibrarySerializer


class LibraryCreateView(generics.CreateAPIView):
    """
    API endpoint for creating a new Library.

    Inheriting from generics.CreateAPIView gives us a working POST
    endpoint with very little code. Behind the scenes, DRF's CreateAPIView
    handles:
        1. Receiving the POST request and parsing the JSON body.
        2. Passing that data into the serializer (LibrarySerializer) for
           validation (e.g. checking 'name' isn't blank, checking
           uniqueness constraints, running our custom validate_name and
           validate_enabled_modules methods).
        3. If validation passes, calling serializer.save(), which in turn
           calls the create() method we wrote on LibrarySerializer — this
           is where the library_id gets generated and the Library row
           actually gets inserted into Postgres.
        4. Returning a 201 Created response with the newly created
           Library's data (serialized back to JSON), including the
           generated library_id and created_at timestamp.
        5. If validation fails, automatically returning a 400 Bad Request
           response with details about what went wrong, in this format:
               {"name": ["This field may not be blank."]}

    We only need to tell it two things: which queryset it's working with,
    and which serializer to use for validating/creating data.
    """

    # queryset tells DRF which model/table this view operates on. For a
    # pure "create" endpoint this isn't strictly used to fetch data (we're
    # not listing or retrieving existing rows here), but DRF's generic
    # views expect it to be defined as part of their standard interface.
    queryset = Library.objects.all()

    # serializer_class tells the view which serializer handles validating
    # incoming data and converting the resulting Library instance back
    # into JSON for the response.
    serializer_class = LibrarySerializer


class LibraryListView(generics.ListAPIView):
    """
    API endpoint for listing all existing Library records.

    generics.ListAPIView handles GET requests and:
        1. Fetches the queryset defined below (every Library row).
        2. Passes that queryset into the serializer with many=True
           (handled internally by DRF), which converts each Library
           instance into a JSON object.
        3. Wraps the result in a 200 OK response as a JSON array, e.g.:
               [
                   {"library_id": "...", "name": "Greenfield Library", ...},
                   {"library_id": "...", "name": "Riverside Library", ...}
               ]

    Note that we reuse the exact same LibrarySerializer used for creation.
    This works because the serializer's `fields` list already includes
    every field we want returned, and `read_only_fields` only affects
    what the client is allowed to SEND, not what gets returned when
    serializing existing data — so it's perfectly suited for both
    reading and writing.
    """

    # Defines the base queryset this view will return. .all() means every
    # row in the library table, in whatever default order Postgres
    # returns them (we can add explicit ordering later if needed, e.g.
    # ordering by name or created_at).
    queryset = Library.objects.all()

    # Reusing LibrarySerializer here, for the reasons explained above.
    serializer_class = LibrarySerializer
