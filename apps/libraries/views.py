from rest_framework import generics
from .models import Library
from .serializers import LibrarySerializer
from core.permissions import (
    IsAdminUser,
    #  IsAdminOrLibrarian,
    #  IsSameUserOrAdmin,
    #  IsSameUserOrAdminOrLibrarian,
)
from core.search import filter_by_tokens


class LibraryCreateView(generics.CreateAPIView):
    permission_classes = [IsAdminUser]
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
    permission_classes = [IsAdminUser]
    """
    API endpoint for listing all existing Library records.
    generics.ListAPIView handles GET requests and:
        1. Fetches the queryset defined by get_queryset() below (every
           Library row, optionally narrowed by a `?name=` search).
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

    SEARCH: ?name=greenfield
        Uses the same tokenized, punctuation/order-insensitive matching
        as books/members/users search (see core/search.py) - e.g.
        ?name=Green Library and ?name=Library, Green would both match a
        library named "Greenfield Library". Omit the param to get every
        library, unfiltered.
    """

    # Reusing LibrarySerializer here, for the reasons explained above.
    serializer_class = LibrarySerializer

    def get_queryset(self):
        """
        WHY THIS IS A METHOD INSTEAD OF A STATIC `queryset = ...`
        CLASS ATTRIBUTE:
            A class attribute like `queryset = Library.objects.all()` is
            built ONCE, when Python first reads the class definition -
            it has no way to see `self.request` at that point, because
            no request exists yet. Search needs to read
            `request.query_params` (i.e. what's after the `?` in the
            URL), which is only available once an actual HTTP request
            comes in. Overriding `get_queryset()` instead means DRF
            calls this method FRESH on every single request, with
            `self.request` already populated - giving us access to that
            request's specific query params.
        """
        # .order_by("name") gives predictable, alphabetical results -
        # without an explicit ordering, Postgres doesn't guarantee any
        # particular row order across requests.
        queryset = Library.objects.all().order_by("name")

        name_query = self.request.query_params.get("name")
        if name_query:
            queryset = filter_by_tokens(queryset, "name", name_query)

        return queryset


class LibraryUpdateView(generics.UpdateAPIView):
    permission_classes = [IsAdminUser]
    """
    API endpoint for updating an existing Library's details.
 
    generics.UpdateAPIView gives us both PUT and PATCH out of the box:
        - PUT   → full update: client must send all editable fields.
                  Missing fields will fail validation.
        - PATCH → partial update: client sends only the fields they want
                  to change. All other fields stay as they are.
 
    The update flow:
        1. The view extracts the library_id from the URL (e.g.
           /api/libraries/<library_id>/update/).
        2. It fetches the matching Library row from the database using
           get_queryset() and lookup_field. If no row is found, DRF
           automatically returns a 404 Not Found.
        3. The incoming request data is passed to LibrarySerializer,
           which validates it (running field-level validators, uniqueness
           checks, etc.).
        4. If validation passes, serializer.save() calls our update()
           method, which sets the changed fields and calls instance.save()
           to write the UPDATE SQL to Postgres.
        5. Returns a 200 OK response with the full updated Library object
           serialized as JSON — including the unchanged read-only fields
           library_id and created_at.
        6. If validation fails, returns 400 Bad Request with error details.
 
    What CAN be updated (all editable fields):
        name, logo, url, primary_color, secondary_color, enabled_modules
 
    What CANNOT be updated (read_only_fields on the serializer):
        library_id, created_at
    """

    queryset = Library.objects.all()
    serializer_class = LibrarySerializer

    # By default, DRF's UpdateAPIView looks up objects using `pk` as the
    # URL keyword argument. Our primary key column is `library_id`, so we
    # override both lookup_field (the model field to filter on) and
    # lookup_url_kwarg (the name of the URL parameter to read the value
    # from) to match.
    lookup_field = "library_id"
    lookup_url_kwarg = "library_id"
