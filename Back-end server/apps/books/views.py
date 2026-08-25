"""
books/views.py

Every view in this file follows the same overall pattern:
    1. Check permissions (who's allowed to call this endpoint at all)
    2. Build/narrow a queryset (which rows from `books` are relevant)
    3. Validate any input (via a serializer)
    4. Do the work (query, aggregate, write, etc.)
    5. Return a Response, which DRF turns into JSON for the client
"""

import csv
import io

from django.db.models import Avg, Count, Max, Min
from django.http import HttpResponse
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import generics, status
from rest_framework.filters import OrderingFilter, SearchFilter
from rest_framework.pagination import PageNumberPagination
from rest_framework.parsers import MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView

from core.permissions import (
    IsAdminUser,
    # IsAdminOrLibrarian,
    # IsSameUserOrAdmin,
    # IsSameUserOrAdminOrLibrarian,
)

from .filters import BookFilter
from .models import Book
from .serializers import BookBulkUpdateSerializer, BookSerializer


class MemberPagination(PageNumberPagination):
    """Caps every books list query at 50 rows per page."""

    page_size = 50
    max_page_size = 50


# Create your views here.


class BookListView(generics.ListAPIView):
    """
    API endpoint for listing Book records - now with search, filter,
    and sort support layered on top of the plain listing.

    generics.ListAPIView handles GET requests and:
        1. Fetches the queryset defined below (every Book row, or a
           narrowed subset once filters are applied).
        2. Passes that queryset into the serializer with many=True
           (handled internally by DRF), which converts each Book
           instance into a JSON object.
        3. Wraps the result in a 200 OK response as a JSON array.

    HOW SEARCH / FILTER / SORT ALL FIT TOGETHER:
        `filter_backends` is a list of "queryset narrowing" strategies
        that DRF runs in order, each one further narrowing whatever the
        previous one produced. So a request like:

            GET /api/books/?search=potter&genre=Fantasy&ordering=-publication_year

        runs like this:
            1. SearchFilter narrows to books where "potter" appears in
               title/subtitle/authors/isbn_10/isbn_13
            2. DjangoFilterBackend (using BookFilter) further narrows
               to just genre="Fantasy" from what's left
            3. OrderingFilter sorts what's left by publication_year,
               descending
            4. Pagination slices the final sorted list into pages of 50

    SINGLE-FIELD vs MULTI-FIELD SEARCH:
        There are two different "search" tools available on this same
        endpoint, and it's worth knowing when each applies:

        - `?search=term` (SearchFilter): ONE term, OR'd across several
          fields at once. Use this when you don't know which field the
          term lives in - e.g. "?search=tolkien" might match either the
          `authors` or `description` field.

        - Field-specific params like `?title=`, `?authors=`, `?genre=`,
          `?isbn_13=`, etc. (BookFilter, in filters.py): each param
          targets exactly ONE named field. Supply just ONE of these for
          a simple single-field search (e.g. `?title=potter`), or
          SEVERAL at once for a more precise, narrowed-down search where
          every condition must match (e.g.
          `?title=potter&authors=rowling&genre=Fantasy`) - since
          django-filter combines every filter param supplied with AND,
          exactly like chaining .filter() calls in the ORM.

        Both tools can even be combined in the same request, since they
        run as separate backends in sequence: `?search=potter&genre=Fantasy`
        first OR-searches for "potter" across multiple fields, then
        narrows further to genre="Fantasy".
    """

    permission_classes = [IsAdminUser]
    queryset = Book.objects.all()
    serializer_class = BookSerializer
    pagination_class = MemberPagination

    filter_backends = [SearchFilter, DjangoFilterBackend, OrderingFilter]

    # --- Search (?search=...) -------------------------------------------------
    # SearchFilter runs a case-insensitive "contains" (ILIKE '%term%')
    # match across every field listed here, OR'd together - i.e. a book
    # matches if ANY of these fields contains the search term.
    # Multiple words in the query (e.g. "?search=harry potter") are
    # ANDed against each other by default: each word must be found
    # SOMEWHERE across these fields (not necessarily in the same field).
    search_fields = ["title", "subtitle", "isbn_10", "isbn_13", "authors"]

    # --- Filter (?genre=..., ?publication_year_min=..., etc.) -------------------
    # Delegates to the BookFilter class we defined in filters.py.
    filterset_class = BookFilter

    # --- Sort (?ordering=title or ?ordering=-title for descending) -------------------
    # Only fields listed here are allowed to be sorted on - this is a
    # deliberate whitelist, since letting clients sort by ANY field
    # (via unrestricted raw field names) can leak information about
    # fields you didn't intend to expose that way.
    ordering_fields = ["publication_year", "title", "created_at"]
    # Default sort applied when the client doesn't specify `?ordering=`.
    ordering = ["title"]


class BookCreateView(generics.CreateAPIView):
    permission_classes = [IsAdminUser]
    queryset = Book.objects.all()
    serializer_class = BookSerializer


class BookUpdateView(generics.UpdateAPIView):
    permission_classes = [IsAdminUser]
    queryset = Book.objects.all()
    serializer_class = BookSerializer
    # By default, DRF's UpdateAPIView looks up objects using `pk` as the
    # URL keyword argument. Our primary key column is `book_id`, so we
    # override both lookup_field (the model field to filter on) and
    # lookup_url_kwarg (the name of the URL parameter to read the value
    # from) to match.
    lookup_field = "book_id"
    lookup_url_kwarg = "book_id"


class BookStatsView(APIView):
    """
    GET /api/books/stats/

    Returns aggregate statistics about the whole `books` table. This is
    a plain APIView (not a generics.* view) because the response isn't
    "a list of Book objects" or "a single Book object" - it's a custom
    summary shape that doesn't map onto a serializer for the Book model
    at all.

    KEY CONCEPT - aggregate() vs annotate():
        - `.aggregate(...)` collapses an ENTIRE queryset down into ONE
          dict of summary values (e.g. one total count, one average).
        - `.annotate(...)` computes a value PER ROW (or per GROUP, when
          combined with `.values()` first) and returns a queryset, not
          a single dict.
        Below, we use `.aggregate()` for single overall numbers, and
        `.values(...).annotate(...)` for "count per group" breakdowns
        (the classic Django equivalent of SQL's GROUP BY).
    """

    permission_classes = [IsAdminUser]

    def get(self, request):
        total_books = Book.objects.count()

        # --- Single-value aggregates -----------------------------------------
        year_stats = Book.objects.aggregate(
            earliest_year=Min("publication_year"),
            latest_year=Max("publication_year"),
            average_year=Avg("publication_year"),
        )

        # --- Books per genre (GROUP BY genre) --------------------------------
        # .values("genre") groups rows by distinct genre values; the
        # subsequent .annotate(count=Count(...)) then counts how many
        # rows fall into each group. .order_by("-count") sorts the
        # groups from most to least common.
        books_by_genre = list(
            Book.objects.values("genre")
            .annotate(count=Count("book_id"))
            .order_by("-count")
        )

        # --- Data completeness ------------------------------------------------
        # How many books are missing an ISBN entirely? Useful for
        # spotting incomplete catalog data.
        missing_isbn_13 = Book.objects.filter(isbn_13__isnull=True).count()
        missing_isbn_10 = Book.objects.filter(isbn_10__isnull=True).count()
        missing_description = Book.objects.filter(
            description__isnull=True
        ).count()

        return Response(
            {
                "total_books": total_books,
                "publication_year": {
                    "earliest": year_stats["earliest_year"],
                    "latest": year_stats["latest_year"],
                    # round() is applied here since Avg() over integers
                    # returns a float with many decimal places.
                    "average": (
                        round(year_stats["average_year"], 1)
                        if year_stats["average_year"] is not None
                        else None
                    ),
                },
                "books_by_genre": books_by_genre,
                "data_completeness": {
                    "missing_isbn_13": missing_isbn_13,
                    "missing_isbn_10": missing_isbn_10,
                    "missing_description": missing_description,
                },
            }
        )


class BookExportView(APIView):
    """
    GET /api/books/export/

    Streams every Book row back as a downloadable CSV file, rather than
    JSON. Browsers/clients hitting this endpoint will be prompted to
    save/download the response as a file, because of the
    Content-Disposition header set below.
    """

    permission_classes = [IsAdminUser]

    # The exact columns to include in the CSV, and their order.
    CSV_COLUMNS = [
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

    def get(self, request):
        # HttpResponse (not DRF's Response) is used here because we're
        # returning a raw file, not a JSON-serializable object - DRF's
        # Response expects to be rendered by a DRF renderer (JSON,
        # browsable API, etc.), which isn't what we want for a CSV
        # download.
        response = HttpResponse(content_type="text/csv")
        response["Content-Disposition"] = 'attachment; filename="books_export.csv"'

        # csv.writer writes rows to any "file-like" object - HttpResponse
        # behaves like one (it supports .write()), so we can hand it
        # straight to the writer instead of building the CSV in memory
        # first.
        writer = csv.writer(response)

        # Header row
        writer.writerow(self.CSV_COLUMNS)

        # .iterator() streams rows from the database in batches instead
        # of loading the entire table into memory at once - important
        # once your catalog grows beyond a trivial size.
        for book in Book.objects.all().iterator():
            writer.writerow([getattr(book, column) for column in self.CSV_COLUMNS])

        return response


class BookImportView(APIView):
    """
    POST /api/books/import/
    Content-Type: multipart/form-data
    Body: file=<a .csv file>

    Reads a CSV file (same column layout as BookExportView produces)
    and creates a Book row for every valid row. Invalid rows are
    skipped and reported back in the response instead of failing the
    entire import - this is a deliberate choice: if you upload 500 rows
    and one has a bad ISBN, you'd usually rather get 499 books imported
    plus a clear error list, rather than nothing imported at all.
    """

    permission_classes = [IsAdminUser]
    # MultiPartParser is what allows DRF to understand file uploads
    # (multipart/form-data) rather than only JSON request bodies.
    parser_classes = [MultiPartParser]

    # Columns we accept from the CSV. Deliberately excludes book_id and
    # created_at - those are system-generated (see BookSerializer's
    # read_only_fields), so even if a CSV contains them, we ignore them
    # here and let the serializer/model defaults take over.
    IMPORTABLE_COLUMNS = [
        "title",
        "subtitle",
        "authors",
        "publication_year",
        "genre",
        "description",
        "isbn_13",
        "isbn_10",
        "cover_image",
    ]

    def post(self, request):
        uploaded_file = request.FILES.get("file")
        if uploaded_file is None:
            return Response(
                {"detail": "No file provided. Send it as form field 'file'."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Uploaded files arrive as bytes; csv.DictReader needs text
        # lines, so we decode and wrap it in a text stream. io.TextIOWrapper
        # would also work directly on the file object, but decoding
        # explicitly here is more transparent for learning purposes.
        decoded_text = uploaded_file.read().decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(decoded_text))

        created_count = 0
        failed_rows = []

        # enumerate(reader, start=2) - start=2 because row 1 is the
        # header, so the first data row is "row 2" from a human
        # spreadsheet-reading perspective, which makes error messages
        # easier to map back to the original file.
        for row_number, row in enumerate(reader, start=2):
            # Only pass through columns we actually accept - protects
            # against unexpected/extra CSV columns being interpreted as
            # model fields.
            row_data = {
                column: row.get(column) or None for column in self.IMPORTABLE_COLUMNS
            }

            serializer = BookSerializer(data=row_data)
            if serializer.is_valid():
                serializer.save()
                created_count += 1
            else:
                failed_rows.append({"row": row_number, "errors": serializer.errors})

        return Response(
            {
                "created_count": created_count,
                "failed_count": len(failed_rows),
                "failed_rows": failed_rows,
            },
            status=status.HTTP_201_CREATED if created_count else status.HTTP_400_BAD_REQUEST,
        )


class BookBulkUpdateView(APIView):
    """
    PATCH /api/books/bulk-update/
    Body:
        {
            "book_ids": ["<uuid>", "<uuid>", ...],
            "genre": "Fantasy",          # optional
            "publication_year": 2015     # optional
        }

    Applies the SAME genre and/or publication_year to MULTIPLE books at
    once, identified by an explicit list of book_ids.

    WHY .update() INSTEAD OF LOOPING + .save() PER BOOK:
        Book.objects.filter(...).update(**fields) issues ONE SQL UPDATE
        statement covering every matching row - much faster than
        fetching each Book instance in Python and calling .save() on
        each one individually (which would be N separate UPDATE
        statements for N books).

        The trade-off: .update() bypasses Model.save() entirely, so any
        custom save() logic, signals, or auto_now fields would NOT run.
        Our Book model doesn't have any of those complications, so
        .update() is the right tool here - but it's worth knowing this
        trade-off exists for future models that DO rely on save()-time
        logic.
    """

    permission_classes = [IsAdminUser]

    def patch(self, request):
        serializer = BookBulkUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        validated = serializer.validated_data

        requested_ids = validated["book_ids"]

        # Build a dict of ONLY the fields that were actually supplied -
        # e.g. if the client only sent "genre", we must not accidentally
        # overwrite every matching book's publication_year with None.
        update_fields = {}
        if "genre" in validated:
            update_fields["genre"] = validated["genre"]
        if "publication_year" in validated:
            update_fields["publication_year"] = validated["publication_year"]

        matching_queryset = Book.objects.filter(book_id__in=requested_ids)

        # Figure out which of the requested IDs actually exist, so we
        # can tell the client if some were skipped (e.g. typo'd UUID,
        # already-deleted book) rather than silently ignoring them.
        found_ids = set(matching_queryset.values_list("book_id", flat=True))
        not_found_ids = [
            str(book_id) for book_id in requested_ids if book_id not in found_ids
        ]

        updated_count = matching_queryset.update(**update_fields)

        return Response(
            {
                "updated_count": updated_count,
                "not_found_ids": not_found_ids,
                "applied_fields": update_fields,
            },
            status=status.HTTP_200_OK,
        )