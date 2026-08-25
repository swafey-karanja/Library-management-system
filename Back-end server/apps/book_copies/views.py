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
from django.http import HttpResponse
from datetime import timedelta
from django.db.models import Count, Q
from django.utils import timezone

from rest_framework import generics, status
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.pagination import PageNumberPagination
from rest_framework.filters import SearchFilter, OrderingFilter
from rest_framework.generics import get_object_or_404
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework.parsers import MultiPartParser

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
    """
    POST /api/book-copies/create/

    Accepts EITHER:
      - a single object -> creates ONE copy, response is a single object
        (same shape as a plain "create" endpoint).
      - a JSON array of objects -> creates MANY copies, response is a list.

    Each item ("spec") is either:
      - one specific copy (optionally with its own barcode), or
      - a request for `quantity` copies of one book/library, with
        barcodes auto-generated by BookCopy.save() (see models.py).

    All-or-nothing: nothing is created if any spec is invalid.

    - Single object body -> one copy created, single object response.
    - Array body -> many copies created, list response.
    - A single object with quantity > 1 still returns a LIST, since
      more than one row was actually created — the response shape
      always matches what was actually made, not the request shape.
    """

    def post(self, request, *args, **kwargs):
        is_batch_request = isinstance(request.data, list)
        specs_data = request.data if is_batch_request else [request.data]

        if len(specs_data) == 0:
            return Response(
                {'detail': 'The array must contain at least one item.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Validates every spec before anything is created — all-or-nothing.
        spec_serializer = BookCopyCreateSpecSerializer(data=specs_data, many=True)
        spec_serializer.is_valid(raise_exception=True)
        specs = spec_serializer.validated_data

        # LIBRARY SCOPING (pattern C): there's no existing queryset to
        # filter here — `library` just arrived in the request body, so
        # scoping means VALIDATING it rather than filtering anything.
        # get_user_library_id() returns None for an admin (meaning "no
        # restriction"), so this check is skipped entirely for them.
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

        created_copies = []
        with transaction.atomic():
            for spec in specs:
                quantity = spec['quantity']

                shared_fields = {}
                for optional_field in ('status', 'condition', 'shelf_location', 'acquired_at'):
                    if optional_field in spec:
                        shared_fields[optional_field] = spec[optional_field]

                for _ in range(quantity):
                    copy_data = {
                        'library': spec['library'].pk,
                        'book': spec['book'].pk,
                        **shared_fields,
                    }
                    # Only set acquired_at if provided, otherwise leave None for model save() to handle
                    if 'acquired_at' in spec and spec['acquired_at'] is not None:
                        copy_data['acquired_at'] = spec['acquired_at']
                    # If not provided, don't include it - model's save() will set it

                    # Only reachable when quantity == 1 (validate() above
                    # rejects barcode + quantity > 1) — every other case
                    # leaves barcode out, so BookCopy.save() auto-generates it.
                    if quantity == 1 and spec.get('barcode'):
                        copy_data['barcode'] = spec['barcode']

                    copy_serializer = BookCopySerializer(data=copy_data)
                    copy_serializer.is_valid(raise_exception=True)
                    created_copies.append(copy_serializer.save())

        response_serializer = BookCopySerializer(created_copies, many=True)

        if not is_batch_request and len(created_copies) == 1:
            # Single spec in, single copy out -> single object response,
            # matching what a plain "create" endpoint would return.
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
    """

    def patch(self, request, *args, **kwargs):
        # Validates each item's shape; only the four allowed fields
        # exist on the serializer, so nothing else can sneak through.
        item_serializer = BookCopyBulkUpdateItemSerializer(data=request.data, many=True)
        item_serializer.is_valid(raise_exception=True)
        items = item_serializer.validated_data

        # Reject duplicate ids outright rather than guessing intent.
        requested_ids = [item['id'] for item in items]
        if len(requested_ids) != len(set(requested_ids)):
            return Response(
                {'detail': 'Duplicate id values found in the request body.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # One query for all rows, scoped to the requester's library
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

        # atomic() = all-or-nothing if something goes wrong mid-batch.
        updated_copies = []
        with transaction.atomic():
            for item in items:
                copy = copies_by_id[item['id']]

                # Only touch fields actually present in this item.
                changed_fields = [
                    field_name
                    for field_name in BULK_UPDATABLE_FIELDS
                    if field_name in item
                ]
                for field_name in changed_fields:
                    setattr(copy, field_name, item[field_name])

                # update_fields restricts the SQL UPDATE to just these
                # columns — extra safety net beyond the serializer.
                copy.save(update_fields=changed_fields)
                updated_copies.append(copy)

        response_serializer = BookCopySerializer(updated_copies, many=True)
        return Response(response_serializer.data, status=status.HTTP_200_OK)


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

    def get(self, request, *args, **kwargs):
        # Runs the queryset through every filter_backend above — same
        # as ListAPIView does internally, just without pagination.
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
        serializer = self.get_serializer(queryset, many=True)

        # Plain Django HttpResponse (not DRF's Response) since we're
        # writing a raw file body, not a JSON API response.
        response = HttpResponse(content_type='text/csv')
        response['Content-Disposition'] = 'attachment; filename="book_copies_export.csv"'

        # HttpResponse is file-like, so csv.writer can write straight into it.
        writer = csv.writer(response)
        writer.writerow(self.EXPORT_FIELDS)
        for row in serializer.data:
            writer.writerow([row.get(field, '') for field in self.EXPORT_FIELDS])

        return response

    def _export_json(self, queryset):
        serializer = self.get_serializer(queryset, many=True)

        response = HttpResponse(content_type='application/json')
        response['Content-Disposition'] = 'attachment; filename="book_copies_export.json"'

        # JSONRenderer (not json.dumps) correctly handles the UUID/
        # datetime types already present in serializer.data.
        from rest_framework.renderers import JSONRenderer
        response.write(JSONRenderer().render(serializer.data))
        return response


class BookCopyImportView(APIView):
    permission_classes = [IsAdminOrLibrarian]
    """
    POST /api/book-copies/import/ — CSV upload (multipart/form-data,
    field "file"). Required columns: library, book, barcode. Optional:
    status, condition, shelf_location, acquired_at.

    Upsert by barcode: existing row -> update, unmatched -> create.
    Best-effort: invalid rows are reported but don't block valid ones.

    LIBRARY SCOPING (patterns B + C together): barcode is globally
    unique, so a CSV row could technically match an existing copy that
    belongs to a DIFFERENT library. Two separate checks are needed:
      B) the "does this barcode already exist" lookup is scoped, so a
         librarian's CSV can't silently edit another library's copy
         just because the barcode string happens to match.
      C) on CREATE, the row's own `library` column is validated
         against the requester's library, same reasoning as
         BookCopyCreateView above.
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

        # Resolved once, outside the loop — None for an admin (no restriction).
        requesting_user_library_id = get_user_library_id(request.user)

        created_count = 0
        updated_count = 0
        errors = []

        # Row numbers start at 2 (row 1 is the header) to match what
        # a user sees if they open the CSV in a spreadsheet.
        for row_number, row in enumerate(reader, start=2):
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

            # Only include optional columns if non-empty, so a blank
            # cell means "don't change this" on update, not "wipe it".
            for column in self.OPTIONAL_COLUMNS:
                value = (row.get(column) or '').strip()
                if value:
                    row_data[column] = value

            # LIBRARY SCOPING (pattern B): scoped lookup, not a bare
            # BookCopy.objects.filter(barcode=barcode) — a non-admin
            # can't "find" (and therefore can't silently update) a
            # copy belonging to a different library, even if its
            # barcode happens to match a row in this CSV.
            existing_copy = scope_queryset_to_library(
                BookCopy.objects.filter(barcode=barcode), request.user
            ).first()

            if existing_copy:
                # instance=existing_copy makes the uniqueness check on
                # barcode correctly ignore this row's own value.
                serializer = BookCopySerializer(existing_copy, data=row_data, partial=True)
            else:
                # LIBRARY SCOPING (pattern C): this is a CREATE, so
                # validate the row's own `library` column instead —
                # same reasoning as BookCopyCreateView above.
                if requesting_user_library_id is not None and row_data['library'] != str(requesting_user_library_id):
                    errors.append({
                        'row': row_number,
                        'barcode': barcode,
                        'errors': {'Operation cannot be completed'},
                    })
                    continue
                serializer = BookCopySerializer(data=row_data)

            if not serializer.is_valid():
                errors.append({
                    'row': row_number,
                    'barcode': barcode,
                    'errors': serializer.errors,
                })
                continue

            # No outer transaction.atomic() around the whole loop —
            # we want valid rows to persist even if others fail.
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