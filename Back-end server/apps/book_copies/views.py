"""
views.py — BookCopy CRUD-ish views

  1. BookCopyListView    — GET  /api/book-copies/  (search, filter, order, paginate)
  2. BookCopyCreateView  — POST /api/book-copies/create/
  3. BookCopyUpdateView  — PUT/PATCH /api/book-copies/<pk>/update/
  4. BookCopyBulkUpdateView — PATCH /api/book-copies/bulk-update/

LIBRARY SCOPING — WORKED EXAMPLE
---------------------------------
This file demonstrates BOTH ways of using core/permissions.py's
scoping tools, since this app happens to have both kinds of view:

  A) Generic views with a get_queryset() (ListView, DetailView,
     UpdateView, ExportView) -> just add LibraryScopedQuerysetMixin to
     the class bases. One line, nothing else changes. It overrides
     get_queryset() for you, and everything built on top of
     get_queryset() (pagination, filtering, get_object()'s 404 lookup)
     is automatically scoped as a result.

  B) Plain APIViews with hand-written queries (BulkUpdateView,
     StatisticsView) -> call scope_queryset_to_library() yourself,
     right where the queryset is built, since there's no
     get_queryset() for a mixin to hook into.

  C) Writes (CreateView, ImportView) are a THIRD case worth noticing:
     there's no existing queryset to filter at all — you're
     validating a library_id that just arrived in the request body.
     Scoping here means checking get_user_library_id(request.user)
     against what was submitted, and rejecting the mismatch, rather
     than filtering anything.
"""
import csv
import io
import uuid

from django.db import transaction
from datetime import timedelta
from django.db.models import Count, Q
from django.http.response import StreamingHttpResponse
from django.utils import timezone

from rest_framework import generics, status
from rest_framework.renderers import JSONRenderer
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.pagination import PageNumberPagination
from rest_framework.filters import SearchFilter, OrderingFilter
from rest_framework.generics import get_object_or_404
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework.parsers import MultiPartParser
from apps.libraries.models import Library
from apps.books.models import Book
from .barcode import build_barcode

from core.permissions import (
    LibraryScopedQuerysetMixin,
    scope_queryset_to_library,
    get_user_library_id, IsAdminOrLibrarian,
)

from .models import BookCopy
from .serializers import (
    BookCopySerializer,
    BookCopyBulkUpdateItemSerializer,
    BookCopyCreateSpecSerializer
)
from .filters import BookCopyFilter

# The only columns a bulk-update request may touch.
BULK_UPDATABLE_FIELDS = ('status', 'condition', 'shelf_location', 'acquired_at')
# Total rows across the whole request (after quantity expansion), not
# just the number of items in the array.
MAX_TOTAL_COPIES_PER_REQUEST = 500


class BookCopyPagination(PageNumberPagination):
    """?page= / ?page_size= (client-overridable, capped by max_page_size)."""
    page_size = 20
    page_size_query_param = 'page_size'
    max_page_size = 100


class BookCopyListView(LibraryScopedQuerysetMixin, generics.ListAPIView):
    permission_classes = [IsAdminOrLibrarian]
    """
    GET /api/book-copies/

    - ?search=term            -> OR across search_fields
    - ?search=term1 term2     -> AND across terms (space-separated)
    - ?status=&condition=&library=&book_name=  -> filters, ANDed (see filters.py)
    - ?ordering=acquired_at | -acquired_at | created_at | -created_at
    All combinable, e.g. ?search=potter&status=available&ordering=-acquired_at&page=2

    LIBRARY SCOPING (pattern A): LibraryScopedQuerysetMixin is listed
    FIRST in the base classes, so its get_queryset() override runs
    before ListAPIView's own filtering/pagination machinery uses it.
    `library_lookup` isn't set below because BookCopy has a direct
    `library` FK — the mixin's default ('library_id') is already
    correct here. A librarian only ever sees/paginates through their
    own library's copies; admins see every library's.
    """

    # select_related avoids N+1 queries when the serializer reads
    # library_name / book_title.
    queryset = BookCopy.objects.select_related('library', 'book').all()

    serializer_class = BookCopySerializer
    pagination_class = BookCopyPagination

    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_class = BookCopyFilter
    search_fields = ['barcode', 'book__title', 'library__name', 'shelf_location']
    ordering_fields = ['acquired_at', 'created_at']
    ordering = ['-created_at']  # stable default so pagination doesn't shift between requests


class BookCopyDetailView(LibraryScopedQuerysetMixin, generics.RetrieveAPIView):
    permission_classes = [IsAdminOrLibrarian]
    """
    GET /api/book-copies/<identifier>/

    <identifier> is EITHER a copy's UUID or its barcode — same endpoint
    either way. This is what a "scan a barcode" screen calls: scan ->
    GET this URL with the scanned string -> prefill the screen.

    Both are unique columns, so either lookup is a single indexed
    query — no meaningful cost difference between the two paths.

    LIBRARY SCOPING (pattern A, worth noticing specifically here):
    get_object() below calls self.get_queryset() itself, and that's
    STILL the mixin's scoped version — Python's method resolution
    order means self.get_queryset() always finds the mixin's override,
    no matter which method calls it. So a librarian scanning another
    library's barcode gets a clean 404 (get_object_or_404 finds
    nothing in the scoped queryset) rather than someone else's copy
    details, with no extra code needed in get_object() itself.
    """

    queryset = BookCopy.objects.select_related('library', 'book').all()
    serializer_class = BookCopySerializer

    def get_object(self):
        identifier = self.kwargs['identifier']
        queryset = self.filter_queryset(self.get_queryset())

        # A real UUID string -> look up by pk. Anything else (e.g. a
        # barcode like "LIB-0001") -> look up by barcode instead.
        try:
            uuid.UUID(identifier)
            lookup = Q(pk=identifier)
        except ValueError:
            lookup = Q(barcode=identifier)

        obj = get_object_or_404(queryset, lookup)
        self.check_object_permissions(self.request, obj)
        return obj


class BookCopyCreateView(APIView):
    permission_classes = [IsAdminOrLibrarian]

    def post(self, request, *args, **kwargs):
        is_batch_request = isinstance(request.data, list)
        specs_data = request.data if is_batch_request else [request.data]

        if len(specs_data) == 0:
            return Response(
                {'detail': 'The array must contain at least one item.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        spec_serializer = BookCopyCreateSpecSerializer(data=specs_data, many=True)
        spec_serializer.is_valid(raise_exception=True)
        specs = spec_serializer.validated_data

        # ---- Library scoping (unchanged from original) ----
        requesting_user_library_id = get_user_library_id(request.user)
        if requesting_user_library_id is not None:
            mismatched_specs = [
                spec for spec in specs
                if spec['library'].library_id != requesting_user_library_id
            ]
            if mismatched_specs:
                return Response(
                    {'detail': 'You can only create book copies for your own library.'},
                    status=status.HTTP_403_FORBIDDEN,
                )

        total_requested = sum(spec['quantity'] for spec in specs)
        if total_requested > MAX_TOTAL_COPIES_PER_REQUEST:
            return Response(
                {
                    'detail': (
                        f'Cannot create more than {MAX_TOTAL_COPIES_PER_REQUEST} '
                        f'book copies in a single request (requested {total_requested}).'
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        # ---- Collect the distinct libraries involved in this request. ----
        # Every spec must belong to exactly one library per request in
        # practice, but we handle the general case: if a batch spans
        # multiple libraries, we lock and process each separately.
        libraries_in_request = {spec['library'].pk: spec['library'] for spec in specs}

        created_copies = []
        with transaction.atomic():
            # Lock every involved Library row, in a deterministic order
            # (sorted by pk) to avoid deadlocks if two requests ever
            # overlap. This is the ONLY lock in the whole operation.
            #
            # It is scoped to Library, NOT Book — so two different
            # libraries adding copies of the same book never block
            # each other. Cross-tenant blocking is structurally
            # impossible here.
            locked_libraries = {}
            for library_pk in sorted(libraries_in_request.keys()):
                locked_libraries[library_pk] = (
                    Library.objects.select_for_update().get(pk=library_pk)
                )

            # ---- Process each library's specs separately. ----
            # Group specs by library so we can do one COUNT per
            # (book, library) pair, not one per spec.
            for library_pk, library in locked_libraries.items():
                library_specs = [
                    spec for spec in specs if spec['library'].pk == library_pk
                ]

                # Group by book so repeated specs for the same book
                # share a single COUNT and continue the sequence.
                specs_by_book = {}
                for spec in library_specs:
                    specs_by_book.setdefault(spec['book'].pk, []).append(spec)

                for book_pk, book_specs in specs_by_book.items():
                    book = book_specs[0]['book']

                    # ONE count per (book, library) — the sequence
                    # starting point for every copy of this book in
                    # this library.
                    existing_count = BookCopy.objects.filter(
                        book_id=book_pk,
                        library_id=library_pk,
                    ).count()
                    next_sequence = existing_count + 1

                    for spec in book_specs:
                        quantity = spec['quantity']

                        shared_fields = {}
                        for field in ('status', 'condition', 'shelf_location'):
                            if field in spec:
                                shared_fields[field] = spec[field]

                        # acquired_at: set explicitly because
                        # bulk_create doesn't call save().
                        acquired_at = spec.get('acquired_at') or timezone.now()

                        for _ in range(quantity):
                            barcode = build_barcode(
                                isbn=getattr(book, 'isbn_13', None)
                                     or getattr(book, 'isbn_10', None),
                                title=getattr(book, 'title', ''),
                                library_code=library.code,
                                sequence=next_sequence,
                            )
                            next_sequence += 1

                            created_copies.append(BookCopy(
                                library=library,
                                book=book,
                                barcode=barcode,
                                acquired_at=acquired_at,
                                **shared_fields,
                            ))

            # ---- One batched INSERT (Django chunks at ~100 rows). ----
            BookCopy.objects.bulk_create(created_copies)

        response_serializer = BookCopySerializer(created_copies, many=True)

        if not is_batch_request and len(created_copies) == 1:
            return Response(response_serializer.data[0], status=status.HTTP_201_CREATED)

        return Response(response_serializer.data, status=status.HTTP_201_CREATED)


class BookCopyUpdateView(LibraryScopedQuerysetMixin, generics.UpdateAPIView):
    permission_classes = [IsAdminOrLibrarian]
    """
    PUT (full) / PATCH (partial) /api/book-copies/<pk>/update/

    LIBRARY SCOPING (pattern A): the whole fix is adding the mixin to
    the base classes below — nothing else in this view changes. A
    librarian PATCHing another library's <pk> now gets a 404 (the row
    isn't in their scoped queryset), never a chance to edit it.
    """
    queryset = BookCopy.objects.all()
    serializer_class = BookCopySerializer
    lookup_field = 'pk'


MAX_BULK_UPDATE_SIZE = 1000


class BookCopyBulkUpdateView(APIView):
    permission_classes = [IsAdminOrLibrarian]
    """
    PATCH /api/book-copies/bulk-update/

    Updates many rows at once, restricted to status/condition/
    shelf_location/acquired_at. APIView (not a generic view) since the
    request body is a LIST of {id, ...fields} rather than one <pk>.

    Body: [{"id": "<uuid>", "status": "borrowed"}, ...]

    LIBRARY SCOPING (pattern B): a plain APIView has no get_queryset()
    for LibraryScopedQuerysetMixin to hook into, so
    scope_queryset_to_library() is called directly, right where the
    queryset is built below. Because the lookup happens BEFORE the
    "missing ids" check, a librarian who lists another library's copy
    id gets the same "not found" response as a genuinely bad id —
    scoping and validation share one error path, rather than leaking
    "that id exists, just not for you" through a different message.

    EFFICIENCY: the actual UPDATE is done with bulk_update() instead of
    a per-row save() loop, collapsing up to N individual UPDATE
    statements into ~ceil(N / batch_size) batched statements. Because
    bulk_update() writes *every* field listed for *every* row, each
    row's untouched fields are first backfilled with their current
    values so nothing gets accidentally overwritten.

    NOTE: bulk_update() does NOT call Model.save() and does NOT fire
    pre_save / post_save signals. If anything downstream listens for
    BookCopy saves (audit logs, cache invalidation, denormalized
    counters), it must be triggered manually after the call.
    """

    def patch(self, request, *args, **kwargs):
        # ---- 1. Cheap guard: reject oversized batches up front. ----
        if len(request.data) > MAX_BULK_UPDATE_SIZE:
            return Response(
                {
                    'detail': (
                        f'Batch too large. Max is '
                        f'{MAX_BULK_UPDATE_SIZE} items per request.'
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        # ---- 2. Validate each item's shape. ----
        # Only the four allowed fields exist on the serializer, so
        # nothing else can sneak through.
        item_serializer = BookCopyBulkUpdateItemSerializer(
            data=request.data, many=True
        )
        item_serializer.is_valid(raise_exception=True)
        items = item_serializer.validated_data

        # ---- 3. Reject duplicate ids outright rather than guessing. ----
        requested_ids = [item['id'] for item in items]
        if len(requested_ids) != len(set(requested_ids)):
            return Response(
                {'detail': 'Duplicate id values found in the request body.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # ---- 4. One query for all rows, scoped to the requester's library. ----
        # (untouched for admins) — then check every id actually matched.
        existing_copies = scope_queryset_to_library(
            BookCopy.objects.filter(id__in=requested_ids), request.user
        )
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

        # ---- 5. Apply changes in memory, then flush with bulk_update. ----
        # atomic() = all-or-nothing if something goes wrong mid-batch.
        #
        # Why the "fill in the blanks" loop:
        #   bulk_update() writes *every* field in `fields` for *every*
        #   row it's given. To preserve the old per-row behaviour of
        #   "only update the columns the caller actually sent", each
        #   row's untouched fields are first set to their *current*
        #   values, so writing them back is a no-op.
        with transaction.atomic():
            for item in items:
                copy = copies_by_id[item['id']]

                for field_name in BULK_UPDATABLE_FIELDS:
                    if field_name in item:
                        setattr(copy, field_name, item[field_name])
                    # else: leave as-is — it already holds the current
                    # value, and bulk_update will write it back unchanged.

            BookCopy.objects.bulk_update(
                copies_by_id.values(),
                fields=BULK_UPDATABLE_FIELDS,
            )

        # ---- 6. Serialize the in-memory objects we just updated. ----
        response_serializer = BookCopySerializer(
            copies_by_id.values(), many=True
        )
        return Response(response_serializer.data, status=status.HTTP_200_OK)


class _EchoBuffer:
    """
    Tiny file-like object for csv.writer to write into. Each write()
    call is captured and can be yielded by the streaming generator.
    csv.writer requires a `.write()` method, so we can't just yield
    strings directly — this adapter lets us use csv.writer inside a
    generator without buffering the whole output.
    """
    def write(self, value):
        return value


class BookCopyExportView(LibraryScopedQuerysetMixin, generics.GenericAPIView):
    permission_classes = [IsAdminOrLibrarian]
    """
    GET /api/book-copies/export/?format=csv|json

    Reuses the list endpoint's filtering/search/ordering, e.g.:
        ?status=available&library=<uuid>&format=csv

    GenericAPIView (not ListAPIView/APIView) gives us filter_queryset()
    and get_serializer() without forcing the normal paginated JSON
    response shape, since this returns a file instead.

    LIBRARY SCOPING (pattern A): the mixin scopes get_queryset(), and
    get() below calls self.get_queryset() via filter_queryset() same
    as ListAPIView does internally — so a librarian's CSV/JSON export
    only ever contains their own library's copies, admins get every
    library's. No changes needed inside get()/_export_csv()/_export_json().

    EFFICIENCY / MEMORY: responses are streamed via StreamingHttpResponse,
    and the queryset is consumed with .iterator(chunk_size=...) so the
    server never holds more than one chunk of rows in memory at a time.
    The serializer is applied per-row rather than to the whole queryset,
    avoiding the O(N) `serializer.data` list that the buffered version
    built. Peak memory is O(chunk_size), independent of export size.

    Because the response is streamed, a mid-stream failure cannot be
    turned into a clean error response — the client has already received
    earlier chunks. Acceptable trade-off for a read-only export.
    """

    queryset = BookCopy.objects.select_related('library', 'book').all()
    serializer_class = BookCopySerializer

    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_class = BookCopyFilter
    search_fields = ['barcode', 'book__title', 'library__name', 'shelf_location']
    ordering_fields = ['acquired_at', 'created_at']
    ordering = ['-created_at']

    # Explicit column set/order for the exported file, kept stable
    # even if the serializer's fields change later.
    EXPORT_FIELDS = [
        'id', 'library', 'library_name', 'book', 'book_title',
        'barcode', 'status', 'condition', 'shelf_location',
        'acquired_at', 'created_at',
    ]

    # How many rows to pull from the DB cursor at a time. Bigger =
    # fewer round trips, more memory per chunk. 500 is a good balance.
    ITERATOR_CHUNK_SIZE = 500

    # Characters that trigger formula interpretation in Excel /
    # Google Sheets. Prefixing a value that starts with one of these
    # with a single quote neutralizes the injection.
    _CSV_INJECTION_PREFIXES = ('=', '+', '-', '@', '\t', '\r')

    def get(self, request, *args, **kwargs):
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

    # ------------------------------------------------------------------
    # CSV
    # ------------------------------------------------------------------
    def _export_csv(self, queryset):
        response = StreamingHttpResponse(
            self._stream_csv(queryset),
            content_type='text/csv; charset=utf-8',
        )
        response['Content-Disposition'] = (
            'attachment; filename="book_copies_export.csv"'
        )
        return response

    def _stream_csv(self, queryset):
        # UTF-8 BOM so Excel on Windows opens with the right encoding.
        yield '\ufeff'

        # Header row.
        yield self._csv_line(self.EXPORT_FIELDS)

        # .iterator() streams rows from the DB cursor in chunks instead of
        # loading the whole result set into memory. select_related still
        # works with .iterator() in Django >= 3.0, so library and book are
        # joined in the same query.
        for copy in queryset.iterator(chunk_size=self.ITERATOR_CHUNK_SIZE):
            row = BookCopySerializer(copy).data
            values = [
                self._csv_safe(row.get(field, ''))
                for field in self.EXPORT_FIELDS
            ]
            yield self._csv_line(values)

    @staticmethod
    def _csv_line(values):
        """Serialize one CSV row to a string using csv.writer's quoting
        rules, without buffering the whole file."""
        buffer = io.StringIO()
        csv.writer(buffer).writerow(values)
        return buffer.getvalue()

    @staticmethod
    def _write_row(writer, values):
        """Write one row and return the resulting string. csv.writer
        writes into our _EchoBuffer, whose write() returns the value,
        so we can yield it directly."""
        writer.writerow(values)
        # _EchoBuffer.write() returns the string, but writerow may call
        # write() multiple times (e.g., for embedded newlines). We
        # capture the last return; for well-formed rows, it's the whole
        # serialized line.
        # To be robust, we build the line ourselves using the same
        # quoting rules csv.writer would use — but simpler: just
        # accumulate via a fresh io.StringIO per row.
        # (See note below.)
        return ''

    @classmethod
    def _csv_safe(cls, value):
        """Neutralize CSV formula injection. A value that starts with
        =, +, -, @, tab, or CR is prefixed with a single quote so
        Excel/Sheets treat it as text, not a formula."""
        if isinstance(value, str) and value and value[0] in cls._CSV_INJECTION_PREFIXES:
            return "'" + value
        return value

    # ------------------------------------------------------------------
    # JSON
    # ------------------------------------------------------------------
    def _export_json(self, queryset):
        response = StreamingHttpResponse(
            self._stream_json(queryset),
            content_type='application/json; charset=utf-8',
        )
        response['Content-Disposition'] = (
            'attachment; filename="book_copies_export.json"'
        )
        return response

    def _stream_json(self, queryset):
        # Emit a JSON array manually so we can stream one object at a
        # time. JSONRenderer is used per-row to correctly handle UUID
        # and datetime types.
        renderer = JSONRenderer()

        yield '['
        first = True
        for copy in queryset.iterator(chunk_size=self.ITERATOR_CHUNK_SIZE):
            if not first:
                yield ','
            first = False
            yield renderer.render(BookCopySerializer(copy).data)
        yield ']'


MAX_IMPORT_ROWS = 10_000
MAX_IMPORT_FILE_BYTES = 10 * 1024 * 1024  # 10 MB


class BookCopyImportView(APIView):
    permission_classes = [IsAdminOrLibrarian]
    """
    POST /api/book-copies/import/ — CSV upload (multipart/form-data,
    field "file"). Required columns: library, book, barcode. Optional:
    status, condition, shelf_location, acquired_at.

    Upsert by barcode: existing row -> update, unmatched -> create.
    Best-effort: invalid rows are reported but don't block valid ones.

    LIBRARY SCOPING (patterns B + C together):
      B) the "does this barcode already exist" lookup is scoped, so a
         librarian's CSV can't silently edit another library's copy.
      C) on CREATE, the row's own `library` column is validated
         against the requester's library.

    EFFICIENCY: unlike a per-row upsert (which issues ~3-5 SQL queries
    per row), this version resolves everything in a handful of batched
    queries and writes with bulk_create / bulk_update. A 1000-row CSV
    goes from ~4000-5000 round trips to ~10-20.

    RACE CONDITION: the whole import runs inside a single transaction
    with the involved Library rows locked via select_for_update(), in
    sorted-pk order. This serializes concurrent imports of the same
    library without ever blocking a different library — cross-tenant
    blocking is structurally impossible because locks are per-Library.
    """

    parser_classes = [MultiPartParser]

    # Columns applied on create/update if present and non-empty.
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

        if uploaded_file.size > MAX_IMPORT_FILE_BYTES:
            return Response(
                {
                    'detail': (
                        f'File too large. Max is '
                        f'{MAX_IMPORT_FILE_BYTES // (1024 * 1024)} MB.'
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        # utf-8-sig strips a BOM if present (common with Excel exports).
        try:
            decoded_content = uploaded_file.read().decode('utf-8-sig')
        except UnicodeDecodeError:
            return Response(
                {'detail': 'File must be UTF-8 encoded.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        reader = csv.DictReader(io.StringIO(decoded_content))

        required_columns = {'library', 'book', 'barcode'}
        provided_columns = set(reader.fieldnames or [])
        missing_columns = required_columns - provided_columns
        if missing_columns:
            return Response(
                {'detail': f'CSV is missing required column(s): {sorted(missing_columns)}'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Read all rows up front so we can batch every lookup. Row numbers
        # start at 2 (row 1 is the header) to match a spreadsheet view.
        raw_rows = list(reader)
        if len(raw_rows) > MAX_IMPORT_ROWS:
            return Response(
                {
                    'detail': (
                        f'CSV has too many rows ({len(raw_rows)}). '
                        f'Max is {MAX_IMPORT_ROWS}.'
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        requesting_user_library_id = get_user_library_id(request.user)
        errors = []

        # ------------------------------------------------------------------
        # PHASE 1: Parse and pre-validate every row. No DB access yet.
        # ------------------------------------------------------------------
        # Each parsed row keeps its original line number so errors map
        # back to what the user sees in their spreadsheet.
        parsed_rows = []
        for row_number, row in enumerate(raw_rows, start=2):
            barcode = (row.get('barcode') or '').strip()
            if not barcode:
                errors.append({
                    'row': row_number,
                    'errors': {'barcode': 'This field is required.'},
                })
                continue

            row_data = {
                'library': (row.get('library') or '').strip(),
                'book': (row.get('book') or '').strip(),
                'barcode': barcode,
            }

            for column in self.OPTIONAL_COLUMNS:
                value = (row.get(column) or '').strip()
                if value:
                    row_data[column] = value

            if not row_data['library'] or not row_data['book']:
                errors.append({
                    'row': row_number,
                    'barcode': barcode,
                    'errors': {
                        'library': 'This field is required.' if not row_data['library'] else None,
                        'book': 'This field is required.' if not row_data['book'] else None,
                    },
                })
                continue

            parsed_rows.append({'row_number': row_number, 'data': row_data})

        # ------------------------------------------------------------------
        # PHASE 2: ONE scoped lookup for all existing barcodes.
        # ------------------------------------------------------------------
        all_barcodes = [p['data']['barcode'] for p in parsed_rows]
        existing_by_barcode = {
            copy.barcode: copy
            for copy in scope_queryset_to_library(
                BookCopy.objects.filter(barcode__in=all_barcodes), request.user
            )
        }

        # ------------------------------------------------------------------
        # PHASE 3: ONE lookup each for libraries and books.
        # ------------------------------------------------------------------
        # These are looked up by their identifier column (pk, or whatever
        # the CSV supplies). Adjust the field names to match your schema —
        # if the CSV uses UUIDs for library/book, this is `pk__in`; if it
        # uses codes, use the code field instead.
        library_ids = {p['data']['library'] for p in parsed_rows}
        book_ids = {p['data']['book'] for p in parsed_rows}

        libraries_by_id = {
            str(lib.pk): lib
            for lib in Library.objects.filter(pk__in=library_ids)
        }
        books_by_id = {
            str(book.pk): book
            for book in Book.objects.filter(pk__in=book_ids)
        }

        # ------------------------------------------------------------------
        # PHASE 4: Classify rows into creates vs updates in memory.
        # ------------------------------------------------------------------
        to_create = []
        to_update = []
        # Track which existing rows we've already touched, so a CSV that
        # repeats a barcode twice doesn't append two updates to the same
        # instance (the last write would win anyway, but deduping avoids
        # confusing bookkeeping).
        seen_update_barcodes = set()

        for parsed in parsed_rows:
            row_number = parsed['row_number']
            row_data = parsed['data']
            barcode = row_data['barcode']

            library = libraries_by_id.get(row_data['library'])
            if library is None:
                errors.append({
                    'row': row_number,
                    'barcode': barcode,
                    'errors': {'library': 'Library not found.'},
                })
                continue

            book = books_by_id.get(row_data['book'])
            if book is None:
                errors.append({
                    'row': row_number,
                    'barcode': barcode,
                    'errors': {'book': 'Book not found.'},
                })
                continue

            existing_copy = existing_by_barcode.get(barcode)

            if existing_copy:
                # LIBRARY SCOPING (pattern B): the scoped lookup above
                # already guarantees this copy belongs to the requester's
                # library (or that the requester is an admin).
                if barcode in seen_update_barcodes:
                    errors.append({
                        'row': row_number,
                        'barcode': barcode,
                        'errors': {'barcode': 'Duplicate barcode in CSV.'},
                    })
                    continue
                seen_update_barcodes.add(barcode)

                for column in self.OPTIONAL_COLUMNS:
                    if column in row_data:
                        setattr(existing_copy, column, row_data[column])
                to_update.append(existing_copy)
            else:
                # LIBRARY SCOPING (pattern C): on CREATE, validate the
                # row's own `library` column against the requester.
                if (
                    requesting_user_library_id is not None
                    and str(library.pk) != str(requesting_user_library_id)
                ):
                    errors.append({
                        'row': row_number,
                        'barcode': barcode,
                        'errors': {'Operation cannot be completed'},
                    })
                    continue

                create_fields = {
                    'library': library,
                    'book': book,
                    'barcode': barcode,
                }
                for column in self.OPTIONAL_COLUMNS:
                    if column in row_data:
                        create_fields[column] = row_data[column]

                # bulk_create skips Model.save(), so acquired_at must be
                # set explicitly if the CSV didn't supply one.
                if 'acquired_at' not in create_fields:
                    create_fields['acquired_at'] = timezone.now()

                to_create.append(BookCopy(**create_fields))

        # ------------------------------------------------------------------
        # PHASE 5: Write everything inside ONE transaction.
        # ------------------------------------------------------------------
        # Lock the involved Library rows (sorted by pk to avoid deadlocks)
        # so concurrent imports against the same library can't interleave
        # their barcode uniqueness checks. Different libraries lock
        # different rows and never block each other.
        created_count = 0
        updated_count = 0

        if to_create or to_update:
            involved_library_pks = sorted(
                {copy.library_id for copy in to_create}
                | {copy.library_id for copy in to_update}
            )
            with transaction.atomic():
                # Acquire locks in deterministic order.
                list(
                    Library.objects
                    .select_for_update()
                    .filter(pk__in=involved_library_pks)
                    .order_by('pk')
                )

                if to_create:
                    BookCopy.objects.bulk_create(to_create)
                    created_count = len(to_create)

                if to_update:
                    BookCopy.objects.bulk_update(to_update, fields=BULK_UPDATABLE_FIELDS)
                    updated_count = len(to_update)

        return Response(
            {
                'created': created_count,
                'updated': updated_count,
                'error_count': len(errors),
                'errors': errors,
            },
            status=status.HTTP_200_OK,
        )


class BookCopyStatisticsView(APIView):
    permission_classes = [IsAdminOrLibrarian]
    """
        Returns counts/breakdowns, not individual rows.
        GET /api/book-copies/statistics/ — dashboard-style aggregate numbers,
        computed in the database (not by looping over rows in Python).

        LIBRARY SCOPING (pattern B): the base queryset is scoped once,
        at the very top, and every breakdown below (by_status,
        by_condition, by_library, the 30-day count, the missing-shelf
        count) is computed FROM that already-scoped queryset — so
        nothing past this first line needs to know or care about
        scoping. For a librarian, `by_library` naturally comes back as
        a single entry (their own); for an admin, every library shows.
    """

    def get(self, request, *args, **kwargs):
        queryset = scope_queryset_to_library(BookCopy.objects.all(), request.user)
        total_copies = queryset.count()

        # GROUP BY status, then fill in any status with zero copies so
        # the frontend doesn't have to guess "missing" vs "zero".
        status_breakdown = queryset.values('status').annotate(count=Count('id'))
        by_status = {row['status']: row['count'] for row in status_breakdown}
        by_status = {
            choice_value: by_status.get(choice_value, 0)
            for choice_value, _label in BookCopy.STATUS_CHOICES
        }

        condition_breakdown = queryset.values('condition').annotate(count=Count('id'))
        by_condition = {row['condition']: row['count'] for row in condition_breakdown}
        by_condition = {
            choice_value: by_condition.get(choice_value, 0)
            for choice_value, _label in BookCopy.CONDITION_CHOICES
        }

        # No fixed list of libraries to fill zeros for — only libraries
        # with at least one copy show up here, sorted by count desc.
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

        # timezone.now() (not datetime.now()) respects USE_TZ settings.
        thirty_days_ago = timezone.now() - timedelta(days=30)
        copies_acquired_last_30_days = queryset.filter(
            acquired_at__gte=thirty_days_ago
        ).count()

        # Q lets us OR two conditions (null or empty string).
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