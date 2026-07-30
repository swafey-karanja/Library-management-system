"""
views.py — BookCopy views
============================

This module defines four views for book_copies:

  1. BookCopyListView    — GET  /api/book-copies/            (list, with
                            search, filtering, ordering, pagination)
  2. BookCopyCreateView  — POST /api/book-copies/create/      (add a copy)
  3. BookCopyUpdateView  — PUT/PATCH /api/book-copies/<pk>/update/
                            (update an existing copy)

A "view" in DRF is the piece of code that actually handles an incoming
HTTP request and decides what to send back. Rather than writing the
request-handling logic (querying the DB, validating input, building a
response) by hand, DRF gives us "generic views" — prebuilt classes
that already know how to do common jobs like "list objects", "create
an object", "update an object". We just tell each one WHAT model/
serializer to use, and customise behaviour where needed.
"""

from django.db import transaction

from rest_framework import generics, status
from rest_framework.views import APIView
from rest_framework.pagination import PageNumberPagination
from rest_framework.filters import SearchFilter, OrderingFilter
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework.response import Response
from datetime import timedelta
from django.db.models import Count, Q
from django.utils import timezone
import csv
import io

from django.db import transaction
from django.http import HttpResponse
from rest_framework.parsers import MultiPartParser

from .models import BookCopy
from .serializers import BookCopySerializer, BookCopyBulkUpdateItemSerializer
from .filters import BookCopyFilter

# The only columns a bulk-update request is allowed to touch. Defined
# once here so both the "how many fields actually changed on this row"
# logic below and any future code that needs the same list stay in
# sync with each other instead of duplicating this list by hand.
BULK_UPDATABLE_FIELDS = ('status', 'condition', 'shelf_location', 'acquired_at')


class BookCopyPagination(PageNumberPagination):
    """
    Controls how results are split into pages.

    PageNumberPagination expects a `?page=` query parameter, e.g.:
        GET /api/book-copies/?page=2

    - `page_size`: how many results are returned per page by default.
    - `page_size_query_param`: lets the CLIENT override the page size
      per-request, e.g. `?page_size=50`. This is optional but handy —
      remove this line if you'd rather enforce a fixed page size.
    - `max_page_size`: caps what a client can request via
      page_size_query_param, so nobody can request `?page_size=100000`
      and force the database to return everything at once.
    """
    page_size = 20
    page_size_query_param = 'page_size'
    max_page_size = 100


class BookCopyListView(generics.ListAPIView):
    """
    GET /api/book-copies/

    Returns a paginated, searchable, filterable, sortable list of
    BookCopy rows.

    --------------------------------------------------------------
    SEARCHING  ->  ?search=<term>
    --------------------------------------------------------------
    Uses DRF's built-in SearchFilter against the fields listed in
    `search_fields` below. A couple of things worth knowing:

      - A SINGLE query: ?search=potter
        Matches any row where "potter" appears (case-insensitively)
        in ANY of the search_fields (barcode, book title, library
        name, shelf location). This is an OR across fields.

      - MULTIPLE queries together: ?search=potter available
        SearchFilter splits the search string on whitespace into
        separate terms ("potter", "available") and requires that
        EVERY term matches SOMEWHERE across the search_fields (an
        AND across terms, with an OR across fields for each term).
        So this example would only match rows where both "potter"
        AND "available" appear somewhere among the searched fields.

    --------------------------------------------------------------
    FILTERING  ->  ?status=&condition=&library=&book_name=
    --------------------------------------------------------------
    Handled by BookCopyFilter (see filters.py). Filters are always
    ANDed together and can be combined with search and ordering
    freely, e.g.:
        ?search=potter&status=available&condition=good&ordering=-acquired_at

    --------------------------------------------------------------
    SORTING  ->  ?ordering=acquired_at | -acquired_at | created_at | -created_at
    --------------------------------------------------------------
    Handled by DRF's OrderingFilter. A leading "-" means descending.
    Only fields listed in `ordering_fields` are allowed, as a safety
    measure — without that restriction, a client could pass any
    column name (including ones you don't want exposed for sorting).
    """

    # Base queryset: every BookCopy row. `select_related('library',
    # 'book')` fetches the related Library and Book rows in the SAME
    # database query (a SQL JOIN) instead of firing a separate query
    # per row when the serializer or filters/search touch
    # library.name / book.title. This avoids the "N+1 queries"
    # performance problem.
    queryset = BookCopy.objects.select_related('library', 'book').all()

    serializer_class = BookCopySerializer
    pagination_class = BookCopyPagination

    # `filter_backends` is the list of "plug-ins" DRF runs (in order)
    # against the queryset before returning results. Each backend
    # inspects the request's query parameters and narrows/reorders the
    # queryset accordingly.
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]

    # Tells DjangoFilterBackend which FilterSet class defines the
    # available filters (status, condition, library, book_name) —
    # see filters.py for how each one works.
    filterset_class = BookCopyFilter

    # Tells SearchFilter which model fields participate in `?search=`.
    # `book__title` and `library__name` use Django's double-underscore
    # syntax to reach through the `book` / `library` foreign keys,
    # same idea as in filters.py.
    search_fields = ['barcode', 'book__title', 'library__name', 'shelf_location']

    # Whitelist of fields that `?ordering=` is allowed to sort by.
    ordering_fields = ['acquired_at', 'created_at']

    # Default ordering applied when the client doesn't specify
    # `?ordering=` at all. Without SOME default ordering, Postgres may
    # return rows in a slightly different order on each request, which
    # makes pagination unreliable (a row could appear twice across two
    # pages, or get skipped).
    ordering = ['-created_at']


class BookCopyCreateView(generics.CreateAPIView):
    """
    POST /api/book-copies/create/

    Adds a new BookCopy. Expects a JSON body containing at least the
    required fields defined on the model, e.g.:

        {
            "library": "<library_uuid>",
            "book": "<book_uuid>",
            "barcode": "LIB-000123",
            "status": "available",
            "condition": "new",
            "shelf_location": "A3-12"
        }

    CreateAPIView handles the full flow for us:
      1. Passes the incoming JSON into BookCopySerializer.
      2. Runs validation (required fields, `unique=True` on barcode,
         valid `choices=` for status/condition, etc).
      3. If valid, saves a new BookCopy row and returns it as JSON
         with a 201 Created status.
      4. If invalid, returns a 400 Bad Request with a JSON body
         describing exactly what failed — we don't have to write any
         of that error-handling ourselves.
    """
    queryset = BookCopy.objects.all()
    serializer_class = BookCopySerializer


class BookCopyUpdateView(generics.UpdateAPIView):
    """
    PUT /api/book-copies/<pk>/update/    (full update)
    PATCH /api/book-copies/<pk>/update/  (partial update)

    Updates an existing BookCopy, identified by its primary key (`pk`,
    i.e. its UUID `id`) in the URL.

    - PUT expects the COMPLETE set of fields and replaces the object.
    - PATCH allows sending only the fields you want to change (e.g.
      just {"status": "borrowed"}), leaving everything else untouched.
      DRF enables this automatically — no extra code needed on our
      side to support both.

    UpdateAPIView handles looking the row up by pk, validating the
    submitted data against BookCopySerializer, saving the changes, and
    returning the updated object as JSON.
    """
    queryset = BookCopy.objects.all()
    serializer_class = BookCopySerializer

    # `lookup_field` tells DRF which model field to match against the
    # URL's <pk> segment. `pk` (short for "primary key") is Django's
    # generic alias for whatever the primary key field actually is —
    # in our case that's BookCopy.id. We're setting it explicitly here
    # for clarity, even though 'pk' is also DRF's default.
    lookup_field = 'pk'

class BookCopyBulkUpdateView(APIView):
    """
    PATCH /api/book-copies/bulk-update/

    Updates MANY BookCopy rows in a single request, but ONLY allows
    changing: status, condition, shelf_location, acquired_at.

    Why not use UpdateAPIView here? UpdateAPIView (and DRF's generic
    views in general) are built around handling ONE object, identified
    by a single <pk> in the URL. A bulk update needs to accept a LIST
    of objects (each with its own id) in the request BODY instead, and
    apply different data to each one — that doesn't fit the generic
    "one URL = one object" pattern, so we drop down to a plain
    `APIView` and write the request-handling logic ourselves.

    Why PATCH rather than POST? PATCH signals "partially update
    existing resource(s)", which matches what this endpoint does
    semantically (not creating anything new). It's a judgment call —
    some APIs use POST for bulk actions like this instead, since the
    endpoint doesn't map to a single resource URL. Either is
    defensible; PATCH was chosen here to stay consistent with the
    single-object update endpoint above.

    Expected request body — a JSON array, e.g.:
        [
            {"id": "<uuid-1>", "status": "borrowed"},
            {"id": "<uuid-2>", "condition": "good", "shelf_location": "B2-04"},
            {"id": "<uuid-3>", "acquired_at": "2025-01-15T10:00:00Z"}
        ]

    Response body: the updated BookCopy rows, serialized the same way
    as the list/detail endpoints (via BookCopySerializer), so the
    client can immediately refresh its UI with the new values without
    a follow-up GET request.
    """

    def patch(self, request, *args, **kwargs):
        # STEP 1 — validate the SHAPE and CONTENT of each item in the
        # incoming list. `many=True` tells DRF the request body is a
        # list of objects, and to run BookCopyBulkUpdateItemSerializer
        # against EACH one individually.
        #
        # Since BookCopyBulkUpdateItemSerializer only declares id,
        # status, condition, shelf_location, and acquired_at, this is
        # what actually enforces "bulk update can only touch these
        # four columns" — barcode/library/book simply have no field to
        # be validated into, so any such values in the request body
        # are silently dropped and never reach the database.
        item_serializer = BookCopyBulkUpdateItemSerializer(data=request.data, many=True)

        # `raise_exception=True` makes DRF automatically return a 400
        # Bad Request (with a detailed per-item error breakdown) if
        # ANY item fails validation, without us writing an if/else for
        # it ourselves.
        item_serializer.is_valid(raise_exception=True)

        # `validated_data` is now a plain Python list of dicts, e.g.
        # [{'id': UUID(...), 'status': 'borrowed'}, ...] — cleaned and
        # type-converted (e.g. the id strings are now real UUID
        # objects, acquired_at strings are now real datetimes).
        items = item_serializer.validated_data

        # STEP 2 — guard against duplicate ids in the same request
        # (e.g. the same copy listed twice with conflicting updates).
        # We reject the whole batch rather than guessing which one the
        # client "really meant" — silently picking one would be
        # surprising/unsafe behaviour for an API to have.
        requested_ids = [item['id'] for item in items]
        if len(requested_ids) != len(set(requested_ids)):
            return Response(
                {'detail': 'Duplicate id values found in the request body.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # STEP 3 — fetch all the rows referenced by the request in ONE
        # query (rather than one query per id in a loop), then check
        # that every requested id actually matched an existing row.
        existing_copies = BookCopy.objects.filter(id__in=requested_ids)
        copies_by_id = {copy.id: copy for copy in existing_copies}

        missing_ids = set(requested_ids) - set(copies_by_id.keys())
        if missing_ids:
            return Response(
                {
                    'detail': 'One or more book copies were not found.',
                    'missing_ids': [str(missing_id) for missing_id in missing_ids],
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        # STEP 4 — apply the updates. `transaction.atomic()` wraps all
        # the saves below in a single database transaction: if
        # anything goes wrong partway through (e.g. an unexpected DB
        # error on row 7 of 10), Postgres rolls back ALL of the
        # changes in this block, rather than leaving the batch
        # half-applied. This keeps the operation "all or nothing".
        updated_copies = []
        with transaction.atomic():
            for item in items:
                copy = copies_by_id[item['id']]

                # Only assign — and only mark as changed — the specific
                # columns that were actually present in THIS item, so
                # that (for example) an item that only sends `status`
                # doesn't overwrite condition/shelf_location/
                # acquired_at with stale/empty values.
                changed_fields = [
                    field_name
                    for field_name in BULK_UPDATABLE_FIELDS
                    if field_name in item
                ]
                for field_name in changed_fields:
                    setattr(copy, field_name, item[field_name])

                # `update_fields=` tells Django to generate an SQL
                # UPDATE that only touches these specific columns,
                # instead of rewriting every column on the row. This
                # is both a small performance win and an extra safety
                # net: even if something upstream went wrong, this
                # save() physically cannot write to barcode/library/
                # book/created_at, because they're not in the list.
                copy.save(update_fields=changed_fields)
                updated_copies.append(copy)

        # STEP 5 — return the updated rows using the SAME serializer
        # as the list/detail endpoints, so the response shape the
        # client receives is consistent everywhere in the API.
        response_serializer = BookCopySerializer(updated_copies, many=True)
        return Response(response_serializer.data, status=status.HTTP_200_OK)


class BookCopyStatisticsView(APIView):
    """
    GET /api/book-copies/statistics/

    Returns aggregate statistics about the book_copies table. Unlike
    the list endpoint, this does NOT return individual rows — it
    returns pre-computed counts and breakdowns, calculated directly in
    the database (not in Python), which is far more efficient than
    pulling every row back and counting them in application code.
    """

    def get(self, request, *args, **kwargs):
        # Base queryset — everything in book_copies. Note we don't
        # need select_related('library', 'book') here like the list
        # view does — we're not serializing individual rows or
        # accessing related fields row-by-row, we're asking the
        # database to do grouping/counting for us instead.
        queryset = BookCopy.objects.all()

        # `.count()` translates to a SQL `SELECT COUNT(*)` — it never
        # loads the actual rows into Python, just the number.
        total_copies = queryset.count()

        # --------------------------------------------------------------
        # BREAKDOWN BY STATUS
        # --------------------------------------------------------------
        # `.values('status')` groups rows by their `status` value (like
        # SQL's GROUP BY), and `.annotate(count=Count('id'))` adds a
        # count of rows in each group. The result is something like:
        #   [{'status': 'available', 'count': 120}, {'status': 'borrowed', 'count': 45}, ...]
        status_breakdown = queryset.values('status').annotate(count=Count('id'))

        # Convert that list of dicts into a simple {status: count} dict
        # for easier consumption on the frontend...
        by_status = {row['status']: row['count'] for row in status_breakdown}

        # ...then fill in any status that had ZERO matching rows. Without
        # this step, a status with no copies simply wouldn't appear in
        # the GROUP BY results at all, which would force the frontend
        # to guess whether "missing" means zero or means "not fetched
        # yet". BookCopy.STATUS_CHOICES (defined in models.py) is our
        # source of truth for every possible status value.
        by_status = {
            choice_value: by_status.get(choice_value, 0)
            for choice_value, _label in BookCopy.STATUS_CHOICES
        }

        # --------------------------------------------------------------
        # BREAKDOWN BY CONDITION
        # --------------------------------------------------------------
        # Same pattern as status above.
        condition_breakdown = queryset.values('condition').annotate(count=Count('id'))
        by_condition = {row['condition']: row['count'] for row in condition_breakdown}
        by_condition = {
            choice_value: by_condition.get(choice_value, 0)
            for choice_value, _label in BookCopy.CONDITION_CHOICES
        }

        # --------------------------------------------------------------
        # BREAKDOWN BY LIBRARY
        # --------------------------------------------------------------
        # We can't "fill in zeros" here the way we did for status/
        # condition above, because there's no fixed, hardcoded list of
        # every library (that lives in the library table and can grow
        # over time) — so we only return libraries that actually have
        # at least one copy.
        #
        # `library__library_id` / `library__name` use the double-
        # underscore syntax to pull fields from the related Library
        # row via the `library` foreign key, without needing a second
        # query per row.
        #
        # `.order_by('-count')` sorts libraries with the most copies
        # first, which is usually the most useful order for a
        # dashboard view.
        library_breakdown = (
            queryset
            .values('library__library_id', 'library__name')
            .annotate(count=Count('id'))
            .order_by('-count')
        )
        by_library = [
            {
                'library_id': row['library__library_id'],
                'library_name': row['library__name'],
                'count': row['count'],
            }
            for row in library_breakdown
        ]

        # --------------------------------------------------------------
        # RECENTLY ACQUIRED
        # --------------------------------------------------------------
        # `timezone.now()` (rather than Python's plain `datetime.now()`)
        # is the Django-recommended way to get the current time, since
        # it respects your project's `USE_TZ` / `TIME_ZONE` settings and
        # avoids naive-vs-aware datetime bugs when comparing against
        # `acquired_at` (a TIMESTAMP column).
        thirty_days_ago = timezone.now() - timedelta(days=30)
        copies_acquired_last_30_days = queryset.filter(
            acquired_at__gte=thirty_days_ago
        ).count()

        # --------------------------------------------------------------
        # DATA QUALITY: copies missing a shelf location
        # --------------------------------------------------------------
        # `shelf_location` is nullable, and could also be stored as an
        # empty string. `Q` objects let us combine multiple conditions
        # with OR (Django's default when you chain .filter() calls, or
        # pass multiple keyword arguments, is AND — Q objects are how
        # you express OR instead).
        copies_missing_shelf_location = queryset.filter(
            Q(shelf_location__isnull=True) | Q(shelf_location__exact='')
        ).count()

        data = {
            'total_copies': total_copies,
            'by_status': by_status,
            'by_condition': by_condition,
            'by_library': by_library,
            'copies_acquired_last_30_days': copies_acquired_last_30_days,
            'copies_missing_shelf_location': copies_missing_shelf_location,
        }

        return Response(data)


class BookCopyExportView(generics.GenericAPIView):
    """
    GET /api/book-copies/export/?format=csv
    GET /api/book-copies/export/?format=json

    Downloads book copies as a file, defaulting to CSV.

    This reuses the exact same filtering/searching/sorting machinery
    as BookCopyListView (same filterset_class, search_fields,
    ordering_fields) — so a client can, for example, export only the
    "available" copies at one library:

        GET /api/book-copies/export/?status=available&library=<uuid>&format=csv

    We extend `generics.GenericAPIView` (rather than plain `APIView`,
    or `ListAPIView` like the list endpoint) because GenericAPIView
    gives us `self.filter_queryset()` and `self.get_serializer()` for
    free — the exact filtering/serialization helpers we want here —
    without ALSO forcing DRF's standard paginated "list response"
    shape on us, since a file download isn't a normal JSON response.
    """

    queryset = BookCopy.objects.select_related('library', 'book').all()
    serializer_class = BookCopySerializer

    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_class = BookCopyFilter
    search_fields = ['barcode', 'book__title', 'library__name', 'shelf_location']
    ordering_fields = ['acquired_at', 'created_at']
    ordering = ['-created_at']

    # The exact columns (and their order) that will appear in the
    # exported file. Defined explicitly, rather than exporting
    # "whatever fields the serializer happens to have", so the export
    # format stays stable and predictable even if the serializer
    # changes later.
    EXPORT_FIELDS = [
        'id', 'library', 'library_name', 'book', 'book_title',
        'barcode', 'status', 'condition', 'shelf_location',
        'acquired_at', 'created_at',
    ]

    def get(self, request, *args, **kwargs):
        # `self.filter_queryset(self.get_queryset())` runs the base
        # queryset through EVERY backend listed in `filter_backends`
        # above (filtering, searching, ordering) in one call — this is
        # exactly what ListAPIView does internally before paginating;
        # we're just doing it manually since we don't want pagination
        # for a file export (we want ALL matching rows in one file).
        queryset = self.filter_queryset(self.get_queryset())

        export_format = request.query_params.get('format', 'csv').lower()

        if export_format == 'csv':
            return self._export_csv(queryset)
        elif export_format == 'json':
            return self._export_json(queryset)

        return Response(
            {'detail': f"Unsupported format '{export_format}'. Use 'csv' or 'json'."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    def _export_csv(self, queryset):
        # `self.get_serializer(...)` is a GenericAPIView helper that
        # builds a BookCopySerializer instance for us (same as calling
        # BookCopySerializer(...) directly) — using it keeps this view
        # consistent with DRF conventions used elsewhere in the app.
        serializer = self.get_serializer(queryset, many=True)

        # HttpResponse is Django's (not DRF's) base response class. We
        # use it here instead of DRF's Response because we're building
        # a raw CSV file body, not a JSON API response.
        response = HttpResponse(content_type='text/csv')

        # `Content-Disposition: attachment; filename=...` is the HTTP
        # header that tells a browser "don't render this inline, offer
        # it as a file download named X" — this is what makes clicking
        # this endpoint's URL in a browser prompt a file save dialog.
        response['Content-Disposition'] = 'attachment; filename="book_copies_export.csv"'

        # csv.writer can write directly into an HttpResponse because
        # HttpResponse implements a `.write()` method, making it
        # "file-like" as far as the csv module is concerned.
        writer = csv.writer(response)
        writer.writerow(self.EXPORT_FIELDS)  # header row

        for row in serializer.data:
            # `row.get(field, '')` guards against a field being absent
            # (shouldn't normally happen here, but keeps this robust if
            # EXPORT_FIELDS and the serializer's fields ever drift out
            # of sync).
            writer.writerow([row.get(field, '') for field in self.EXPORT_FIELDS])

        return response

    def _export_json(self, queryset):
        serializer = self.get_serializer(queryset, many=True)

        response = HttpResponse(
            content_type='application/json',
        )
        response['Content-Disposition'] = 'attachment; filename="book_copies_export.json"'

        # DRF's Response object handles JSON rendering automatically
        # when returned from a view, but since we're building a plain
        # HttpResponse here (to force a file download instead of a
        # normal API response), we render the JSON ourselves via DRF's
        # JSONRenderer, which correctly handles the UUID/datetime types
        # already present in serializer.data.
        from rest_framework.renderers import JSONRenderer
        response.write(JSONRenderer().render(serializer.data))
        return response


class BookCopyImportView(APIView):
    """
    POST /api/book-copies/import/

    Accepts an uploaded CSV file (multipart/form-data, field name
    "file") and creates or updates BookCopy rows from it.

    Expected CSV columns (header row required):
        library, book, barcode, status, condition, shelf_location, acquired_at

    - `library` and `book` must be the UUIDs of existing Library / Book
      rows.
    - `barcode` is required and used as the "match key": if a BookCopy
      with that barcode already exists, it's UPDATED with any other
      non-empty columns present in that row; otherwise a NEW row is
      created. This pattern is often called an "upsert" (update-or-
      insert).
    - `status`, `condition`, `shelf_location`, `acquired_at` are
      optional. Leaving one of these columns empty for a given row
      means "don't change it" on update, or "use the model's default"
      on create.

    This is a "best effort" import: if some rows are invalid, valid
    rows are still imported, and the response lists exactly which rows
    failed and why, so the file's issues can be fixed and re-uploaded
    (rather than the whole file being rejected because of one typo).
    """

    # `MultiPartParser` is what allows DRF to understand
    # multipart/form-data requests — the format browsers/HTTP clients
    # use to upload files (as opposed to a plain JSON body). Without
    # this, `request.FILES` would be empty.
    parser_classes = [MultiPartParser]

    # Columns that, if present and non-empty in a CSV row, are applied
    # on both create and update. `library`, `book`, and `barcode` are
    # handled separately below since they're always required.
    OPTIONAL_COLUMNS = ('status', 'condition', 'shelf_location', 'acquired_at')

    def post(self, request, *args, **kwargs):
        uploaded_file = request.FILES.get('file')
        if uploaded_file is None:
            return Response(
                {'detail': "No file uploaded. Send it as multipart/form-data under the key 'file'."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if not uploaded_file.name.lower().endswith('.csv'):
            return Response(
                {'detail': 'Only .csv files are supported.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Uploaded files arrive as raw bytes; CSV needs text. 'utf-8-sig'
        # (rather than plain 'utf-8') also transparently strips a UTF-8
        # BOM (byte-order-mark) if present — a common artefact when
        # CSV files are exported from Excel, which would otherwise show
        # up as a stray character glued onto the very first column
        # name (e.g. "\ufefflibrary" instead of "library").
        try:
            decoded_content = uploaded_file.read().decode('utf-8-sig')
        except UnicodeDecodeError:
            return Response(
                {'detail': 'File must be UTF-8 encoded.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # `io.StringIO` wraps the decoded text so `csv.DictReader` can
        # read it exactly like a file — DictReader turns each row into
        # a dict keyed by the header row's column names (e.g.
        # {'library': '...', 'book': '...', 'barcode': '...', ...}),
        # rather than a plain list of values we'd have to match up to
        # column positions ourselves.
        reader = csv.DictReader(io.StringIO(decoded_content))

        required_columns = {'library', 'book', 'barcode'}
        provided_columns = set(reader.fieldnames or [])
        missing_columns = required_columns - provided_columns
        if missing_columns:
            return Response(
                {'detail': f'CSV is missing required column(s): {sorted(missing_columns)}'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        created_count = 0
        updated_count = 0
        errors = []

        # `enumerate(reader, start=2)` — row numbers start at 2 (not 1
        # or 0) because row 1 is the header row, so the FIRST data row
        # is row 2 in the actual file. This makes any reported error
        # row numbers match exactly what the user sees if they open
        # the CSV in a spreadsheet program.
        for row_number, row in enumerate(reader, start=2):
            barcode = (row.get('barcode') or '').strip()
            if not barcode:
                errors.append({
                    'row': row_number,
                    'errors': {'barcode': 'This field is required.'},
                })
                continue

            # Always-required fields for this row.
            row_data = {
                'library': (row.get('library') or '').strip(),
                'book': (row.get('book') or '').strip(),
                'barcode': barcode,
            }

            # Only include OPTIONAL_COLUMNS in row_data if the CSV cell
            # actually had a non-empty value. This matters a lot for
            # UPDATES specifically: if we always included e.g.
            # "shelf_location": "" for a row where that column was left
            # blank, we'd accidentally WIPE an existing copy's shelf
            # location every time that column is simply omitted from a
            # re-uploaded file. Leaving the key out entirely tells the
            # serializer "don't touch this field", which is what a
            # blank CSV cell should mean.
            for column in self.OPTIONAL_COLUMNS:
                value = (row.get(column) or '').strip()
                if value:
                    row_data[column] = value

            existing_copy = BookCopy.objects.filter(barcode=barcode).first()

            if existing_copy:
                # `partial=True` allows a subset of fields (only the
                # ones present in row_data) — same idea as a PATCH
                # request. Passing `instance=existing_copy` also makes
                # BookCopySerializer's uniqueness check on `barcode`
                # correctly ignore this row's OWN existing barcode
                # value, rather than rejecting it as "already taken by
                # itself".
                serializer = BookCopySerializer(existing_copy, data=row_data, partial=True)
            else:
                serializer = BookCopySerializer(data=row_data)

            if not serializer.is_valid():
                errors.append({
                    'row': row_number,
                    'barcode': barcode,
                    'errors': serializer.errors,
                })
                continue

            # Each `serializer.save()` call is its own database write.
            # We deliberately do NOT wrap the whole file's loop in a
            # single `transaction.atomic()` block here: doing so would
            # mean ANY single bad row rolls back every row already
            # successfully imported before it, which defeats the
            # "best effort / partial success" behaviour this endpoint
            # is designed to have. Each row's own save() is atomic on
            # its own regardless.
            with transaction.atomic():
                serializer.save()

            if existing_copy:
                updated_count += 1
            else:
                created_count += 1

        return Response(
            {
                'created': created_count,
                'updated': updated_count,
                'error_count': len(errors),
                'errors': errors,
            },
            status=status.HTTP_200_OK,
        )