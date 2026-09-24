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

from django.db import IntegrityError, transaction
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
from .barcode import build_prefix, format_barcode, highest_sequence
from .bulk import bulk_update_grouped_by_fields

from core.permissions import (
    LibraryScopedQuerysetMixin,
    scope_queryset_to_library,
    get_user_library_id, IsAdminOrLibrarian,
)

from .models import BookCopy
from .serializers import (
    BookCopySerializer,
    BookCopyBulkUpdateItemSerializer,
    BookCopyCreateSpecSerializer,
    BookCopyImportRowSerializer,
)
from .filters import BookCopyFilter
from apps.inventory.services import sync_inventory_for_copy, sync_inventory_many

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
    POST /api/book-copies/add/

    Body: one spec object, or an array of specs:
        {"library": "<uuid>", "book": "<uuid>", "quantity": 5}
        {"library": "<uuid>", "book": "<uuid>", "barcode": "MY-CUSTOM-1"}

    HOW BARCODES ARE CHOSEN
    -----------------------
    * A spec with an explicit `barcode` (only allowed when quantity is
      1) uses exactly that barcode.
    * Otherwise barcodes are generated as
      '<ISBN-or-slug>-<LIBRARY_CODE>-<NNNN>', continuing from the
      HIGHEST sequence already used for that prefix (see barcode.py
      for why "highest + 1" and not "count + 1").

    HOW IT STAYS SAFE UNDER CONCURRENCY
    -----------------------------------
    Two requests creating copies for the same library at the same
    moment could both read "highest = 5" and both try to insert
    "-0006". To prevent that, every Library row involved is LOCKED
    inside a transaction, so the second request waits until the first
    has committed and then sees the updated numbers.

    The lock is on the Library row (owned by ONE tenant), never on the
    shared Book row, so different libraries never wait on each other.
    """

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

        # ---- Library scoping (pattern C): a librarian may only create
        # copies for their own library. Admins (None) may use any. ----
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

        # ---- Explicit barcodes: check them up front. ----
        # An empty string means "no barcode given" (auto-generate).
        explicit_barcodes = [spec['barcode'] for spec in specs if spec.get('barcode')]

        if len(explicit_barcodes) != len(set(explicit_barcodes)):
            return Response(
                {'detail': 'The same barcode appears more than once in the request.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if explicit_barcodes:
            # ONE query for all of them. Deliberately NOT library-scoped:
            # barcodes are unique across the whole system, so a barcode
            # used by ANY library is unavailable.
            already_used = sorted(
                BookCopy.objects
                .filter(barcode__in=explicit_barcodes)
                .values_list('barcode', flat=True)
            )
            if already_used:
                return Response(
                    {
                        'detail': 'One or more barcodes are already in use.',
                        'barcodes': already_used,
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )

        library_pks = sorted({spec['library'].pk for spec in specs})

        try:
            with transaction.atomic():
                # Lock every involved Library row in a fixed order
                # (sorted by pk) so two overlapping requests can never
                # deadlock by grabbing the same rows in opposite orders.
                #
                # no_key=True -> "FOR NO KEY UPDATE" instead of "FOR
                # UPDATE". Two creators for the SAME library still wait
                # for each other, but unrelated inserts that merely
                # reference this library (a new member, an inventory
                # row, ...) are NOT blocked. Plain FOR UPDATE blocks
                # those too, because Postgres foreign-key checks take a
                # weak lock on the parent row.
                #
                # Use the freshly locked rows (not the copies read
                # during validation) so `code` is current.
                locked_libraries = {
                    library.pk: library
                    for library in (
                        Library.objects
                        .select_for_update(no_key=True)
                        .filter(pk__in=library_pks)
                        .order_by('pk')
                    )
                }

                # Next free sequence number per prefix. Keyed by PREFIX
                # (not by book) so two different books that happen to
                # share a prefix draw from ONE counter.
                next_sequence_by_prefix = {}
                new_copies = []

                for spec in specs:
                    library = locked_libraries[spec['library'].pk]
                    book = spec['book']

                    # Fields the caller may set; anything omitted falls
                    # back to the model default (available / new).
                    shared_fields = {
                        field: spec[field]
                        for field in ('status', 'condition', 'shelf_location')
                        if field in spec
                    }
                    # bulk_create skips save(), so acquired_at must be
                    # set explicitly here.
                    acquired_at = spec.get('acquired_at') or timezone.now()

                    # -- Explicit barcode: use it as-is, consume no number.
                    if spec.get('barcode'):
                        new_copies.append(BookCopy(
                            library=library, book=book,
                            barcode=spec['barcode'],
                            acquired_at=acquired_at, **shared_fields,
                        ))
                        continue

                    # -- Auto-generated barcodes.
                    prefix = build_prefix(
                        isbn=book.isbn_13 or book.isbn_10,
                        title=book.title,
                        library_code=library.code,
                    )

                    if prefix not in next_sequence_by_prefix:
                        # One query the FIRST time we see this prefix.
                        # Filtering by library_id uses an existing index
                        # and keeps the scan inside one tenant's rows.
                        existing_barcodes = (
                            BookCopy.objects
                            .filter(library_id=library.pk, barcode__startswith=f'{prefix}-')
                            .values_list('barcode', flat=True)
                        )
                        next_sequence_by_prefix[prefix] = (
                            highest_sequence(existing_barcodes, prefix) + 1
                        )

                    for _ in range(spec['quantity']):
                        sequence = next_sequence_by_prefix[prefix]
                        next_sequence_by_prefix[prefix] = sequence + 1
                        new_copies.append(BookCopy(
                            library=library, book=book,
                            barcode=format_barcode(prefix, sequence),
                            acquired_at=acquired_at, **shared_fields,
                        ))

                # ONE INSERT for every copy in the request.
                BookCopy.objects.bulk_create(new_copies)

        except IntegrityError:
            # Backstop: the checks above make a barcode clash very
            # unlikely, but if something else slipped a conflicting
            # barcode in (e.g. a direct SQL insert), answer with a
            # clean error instead of a 500. The transaction has already
            # rolled back, so nothing was saved.
            return Response(
                {'detail': 'A barcode conflict occurred. Nothing was created; please retry.'},
                status=status.HTTP_409_CONFLICT,
            )

        # ---- Sync inventory for every (library, book) pair this
        # request touched. Deliberately OUTSIDE the transaction.atomic()
        # block above: sync_inventory() opens its own atomic block per
        # pair (see inventory/services.py), and nesting it inside the
        # Library-row lock we just released would mean holding that
        # lock longer than necessary. Running it after commit also
        # means a slow inventory sync can never make the Library-row
        # lock (which blocks other creators for this library) last any
        # longer than the INSERT itself did.
        #
        # A set() collapses e.g. "50 copies of the same book" down to
        # ONE sync call instead of 50 identical ones.
        touched_pairs = {(copy.library_id, copy.book_id) for copy in new_copies}
        sync_inventory_many(touched_pairs)

        # `new_copies` already hold their library and book objects, so
        # serializing them triggers no extra queries.
        response_serializer = BookCopySerializer(new_copies, many=True)

        if not is_batch_request and len(new_copies) == 1:
            return Response(response_serializer.data[0], status=status.HTTP_201_CREATED)

        return Response(response_serializer.data, status=status.HTTP_201_CREATED)


class BookCopyUpdateSerializer(BookCopySerializer):
    """
    Used only by BookCopyUpdateView (PUT/PATCH on a single copy).

    Same fields as BookCopySerializer, but `library`, `book` and
    `barcode` are additionally read-only here:

    - `library` must stay fixed after creation. A barcode bakes in
      the owning library's code (see barcode.py), so silently moving
      a copy to another library through this endpoint would leave a
      barcode that lies about which library the copy belongs to.
      Transferring a copy between libraries should be a deliberate
      operation of its own, not a side effect of an ordinary update.
    - `book` must stay fixed for the same reason: the barcode also
      encodes the ORIGINAL book's ISBN/title slug.
    - `barcode` must stay fixed once created. `BookCopy.save()` no
      longer auto-generates one (see models.py), so leaving this
      writable would let `PATCH {"barcode": ""}` silently save an
      empty barcode, and an arbitrary new value could collide with
      the sequence numbers the create/import views hand out.
    """

    class Meta(BookCopySerializer.Meta):
        read_only_fields = BookCopySerializer.Meta.read_only_fields + [
            'library', 'book', 'barcode',
        ]


class BookCopyUpdateView(LibraryScopedQuerysetMixin, generics.UpdateAPIView):
    permission_classes = [IsAdminOrLibrarian]
    """
    PUT (full) / PATCH (partial) /api/book-copies/<pk>/update/

    LIBRARY SCOPING (pattern A): the mixin scopes the queryset — a
    librarian PATCHing another library's <pk> gets a 404 (the row
    isn't in their scoped queryset), never a chance to edit it.

    `library`, `book` and `barcode` are read-only on this endpoint —
    see BookCopyUpdateSerializer above for why.
    """
    queryset = BookCopy.objects.all()
    serializer_class = BookCopyUpdateSerializer
    lookup_field = 'pk'

    def perform_update(self, serializer):
        # Inventory counts (available/borrowed/reserved) only depend
        # on `status`. Checking BEFORE save() — via `serializer.
        # validated_data`, which holds only the fields this request
        # actually sent — avoids a wasted sync call on requests that
        # only touched e.g. shelf_location or condition.
        status_changed = 'status' in serializer.validated_data
        copy = serializer.save()
        if status_changed:
            sync_inventory_for_copy(copy)


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
    statements into a small, fixed number of batched statements.

    AVOIDING LOST UPDATES: bulk_update() writes *every* field listed
    in `fields=` for *every* row it's given — not just the fields that
    changed. An earlier version of this view worked around that by
    backfilling each row's untouched fields with their *current*
    value before calling bulk_update(). That has a race condition: if
    another request changes one of those "untouched" fields between
    this view reading the row and its bulk_update() call firing (e.g.
    a member checks the copy out, flipping `status`), that change is
    silently overwritten — even though this request never touched
    `status`.

    Instead, rows are grouped by exactly WHICH fields they changed
    (see bulk.py), and one bulk_update() call is issued per group,
    naming only that group's fields. A field this request never
    touched can then never appear in ANY bulk_update() call, so it can
    never be overwritten — regardless of what changes concurrently.

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

        # ---- 5. Apply only the CHANGED fields in memory, then flush. ----
        # atomic() = all-or-nothing if something goes wrong mid-batch.
        #
        # Each row's untouched fields are left completely alone — not
        # read, not reassigned. `touched_fields` records exactly which
        # attributes this row is changing, so bulk_update_grouped_by_
        # fields() can group rows by that signature and never write a
        # field this request didn't ask to change (see bulk.py).
        rows_to_write = []
        with transaction.atomic():
            for item in items:
                copy = copies_by_id[item['id']]
                touched_fields = set(BULK_UPDATABLE_FIELDS) & item.keys()

                for field_name in touched_fields:
                    setattr(copy, field_name, item[field_name])

                rows_to_write.append((copy, touched_fields))

            bulk_update_grouped_by_fields(BookCopy, rows_to_write)

        # ---- 6. Sync inventory for pairs whose STATUS actually changed. ----
        # total/available/borrowed/reserved only depend on `status`, so
        # a request that only touched e.g. shelf_location needs no sync
        # at all. Outside transaction.atomic() for the same reason as
        # the create view: sync_inventory() manages its own per-pair
        # transaction and shouldn't extend how long this view's locks
        # (implicit, from the UPDATE statements above) are held.
        touched_pairs = {
            (copy.library_id, copy.book_id)
            for copy, touched_fields in rows_to_write
            if 'status' in touched_fields
        }
        sync_inventory_many(touched_pairs)

        # ---- 7. Serialize the in-memory objects we just updated. ----
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

    def perform_content_negotiation(self, request, force=False):
        """
        Bypass DRF's Accept-header / `?format=` negotiation entirely.

        WHY: `?format=csv` was returning a 404 before get() ever ran,
        with no way to reach the "Unsupported format" branch above.
        The cause is a naming collision: DRF's own `APIView.initial()`
        unconditionally reads the query parameter named by
        `URL_FORMAT_OVERRIDE` (default: `"format"`) and uses it to
        pick a *renderer* out of `renderer_classes` (default:
        JSONRenderer + BrowsableAPIRenderer, i.e. formats "json" and
        "api"). "csv" matches neither, so DRF's own negotiation raises
        `Http404` in `initial()` — before this view's `get()`, and
        this endpoint's OWN meaning of `?format=`, are ever reached.

        This view never returns a DRF `Response` for the actual
        export (only for the 400 error case above), so it never
        depends on DRF picking a renderer in the first place. The
        real request format is decided by `get()` above using this
        same `?format=` value — DRF's negotiation is redundant here
        and only exists to get in the way, so it's replaced with a
        fixed, always-succeeding choice.
        """
        return (self.renderer_classes[0](), self.renderer_classes[0].media_type)

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
    Best-effort: a bad row is reported in `errors` and skipped; it does
    NOT stop the good rows from being saved.

    THE FIVE PHASES (everything is batched — no per-row queries)
      1. Parse + validate every row in Python (no database).
      2. One lookup for the barcodes that already exist.
      3. One lookup each for the libraries and books mentioned.
      4. Sort the valid rows into "create" and "update" lists.
      5. Write everything in ONE transaction.

    LIBRARY SCOPING (patterns B + C together):
      B) the "does this barcode already exist" lookup is scoped, so a
         librarian's CSV can't silently edit another library's copy.
      C) on CREATE, the row's own `library` column is validated
         against the requester's library.

    A row is reported as an error (instead of crashing the request) if:
      - a value is invalid (unknown status, bad date, too long, ...)
      - the same barcode appears earlier in the same CSV
      - the library or book doesn't exist
      - a librarian names a library other than their own
      - a NEW barcode is already used by a copy the librarian can't see
        (barcodes are unique across ALL libraries)

    If a concurrent request changes the data between our checks and our
    write (rare), the whole import is rolled back and answered with
    409 so the caller can safely retry.
    """

    parser_classes = [MultiPartParser]

    REQUIRED_COLUMNS = ('library', 'book', 'barcode')
    # Columns applied on create/update if present and non-empty.
    OPTIONAL_COLUMNS = ('status', 'condition', 'shelf_location', 'acquired_at')

    @staticmethod
    def _flatten_errors(serializer_errors):
        """DRF reports {'status': [ErrorDetail(...), ...]}. Flatten each
        list to a single readable string: {'status': '...'}."""
        return {
            field: ' '.join(str(message) for message in messages)
            for field, messages in serializer_errors.items()
        }

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

        missing_columns = set(self.REQUIRED_COLUMNS) - set(reader.fieldnames or [])
        if missing_columns:
            return Response(
                {'detail': f'CSV is missing required column(s): {sorted(missing_columns)}'},
                status=status.HTTP_400_BAD_REQUEST,
            )

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
        # PHASE 1: Validate every row in Python. No database access yet.
        # ------------------------------------------------------------------
        # Row numbers start at 2 (row 1 is the header) so error messages
        # match the line numbers the user sees in their spreadsheet.
        parsed_rows = []
        seen_barcodes = set()

        for row_number, row in enumerate(raw_rows, start=2):
            # Keep only NON-EMPTY cells. For optional columns a blank
            # cell means "leave unchanged", so it must not be validated
            # or written.
            cleaned = {}
            for column in self.REQUIRED_COLUMNS + self.OPTIONAL_COLUMNS:
                value = (row.get(column) or '').strip()
                if value:
                    cleaned[column] = value

            row_serializer = BookCopyImportRowSerializer(data=cleaned)
            if not row_serializer.is_valid():
                errors.append({
                    'row': row_number,
                    'barcode': cleaned.get('barcode'),
                    'errors': self._flatten_errors(row_serializer.errors),
                })
                continue

            # validated_data holds REAL Python types now (UUID objects,
            # timezone-aware datetimes), not raw strings.
            data = row_serializer.validated_data
            barcode = data['barcode']

            # The same barcode twice in one file would make the second
            # INSERT violate the unique constraint and sink the whole
            # batch, so catch it here for creates AND updates alike.
            if barcode in seen_barcodes:
                errors.append({
                    'row': row_number,
                    'barcode': barcode,
                    'errors': {'barcode': 'Duplicate barcode in CSV.'},
                })
                continue
            seen_barcodes.add(barcode)

            parsed_rows.append({'row_number': row_number, 'data': data})

        # ------------------------------------------------------------------
        # PHASE 2: Which barcodes already exist?
        # ------------------------------------------------------------------
        all_barcodes = [parsed['data']['barcode'] for parsed in parsed_rows]

        # (a) Copies this user is ALLOWED to see/edit -> update candidates.
        existing_by_barcode = {
            copy.barcode: copy
            for copy in scope_queryset_to_library(
                BookCopy.objects.filter(barcode__in=all_barcodes), request.user
            )
        }

        # (b) Barcodes that exist but BELONG TO ANOTHER LIBRARY. A
        # librarian can't update those, and can't create them either
        # (barcodes are unique system-wide). Without this check the
        # INSERT would violate the unique constraint and fail the whole
        # import. Only barcodes not already found in (a) need checking,
        # and admins can see everything so they skip this entirely.
        # This query is deliberately NOT scoped; we only read barcode
        # strings from it, never the other library's data.
        taken_by_another_library = set()
        if requesting_user_library_id is not None:
            unseen_barcodes = [b for b in all_barcodes if b not in existing_by_barcode]
            if unseen_barcodes:
                taken_by_another_library = set(
                    BookCopy.objects
                    .filter(barcode__in=unseen_barcodes)
                    .values_list('barcode', flat=True)
                )

        # ------------------------------------------------------------------
        # PHASE 3: ONE lookup each for libraries and books.
        # ------------------------------------------------------------------
        # The serializer already turned these cells into UUID objects, so
        # they match the model's primary keys directly (this also removes
        # a subtle bug where an UPPERCASE uuid string would never have
        # matched the lowercase str(pk) used as a dictionary key before).
        libraries_by_id = {
            library.pk: library
            for library in Library.objects.filter(
                pk__in={parsed['data']['library'] for parsed in parsed_rows}
            )
        }
        books_by_id = {
            book.pk: book
            for book in Book.objects.filter(
                pk__in={parsed['data']['book'] for parsed in parsed_rows}
            )
        }

        # ------------------------------------------------------------------
        # PHASE 4: Sort valid rows into creates and updates (in memory).
        # ------------------------------------------------------------------
        to_create = []
        to_update = []

        for parsed in parsed_rows:
            row_number = parsed['row_number']
            data = parsed['data']
            barcode = data['barcode']

            library = libraries_by_id.get(data['library'])
            if library is None:
                errors.append({
                    'row': row_number, 'barcode': barcode,
                    'errors': {'library': 'Library not found.'},
                })
                continue

            book = books_by_id.get(data['book'])
            if book is None:
                errors.append({
                    'row': row_number, 'barcode': barcode,
                    'errors': {'book': 'Book not found.'},
                })
                continue

            existing_copy = existing_by_barcode.get(barcode)

            if existing_copy:
                # UPDATE. Scoping (pattern B) was already enforced by the
                # scoped lookup in phase 2: a librarian's dictionary only
                # contains their own library's copies.
                # NOTE: only the optional columns are applied; the row's
                # `library` and `book` cells are validated but do not
                # move an existing copy.
                #
                # Only columns PRESENT in this row are set, and only
                # those are recorded as touched. Nothing here reads or
                # relies on an untouched column's current value, so a
                # concurrent change to a column this row didn't mention
                # can never be clobbered when this gets written — see
                # bulk.py for why that matters.
                touched_columns = set(self.OPTIONAL_COLUMNS) & data.keys()
                for column in touched_columns:
                    setattr(existing_copy, column, data[column])
                to_update.append((existing_copy, touched_columns))
                continue

            # CREATE. First, scoping (pattern C).
            if (
                requesting_user_library_id is not None
                and str(library.pk) != str(requesting_user_library_id)
            ):
                errors.append({
                    'row': row_number, 'barcode': barcode,
                    'errors': {'library': 'You can only import copies for your own library.'},
                })
                continue

            if barcode in taken_by_another_library:
                errors.append({
                    'row': row_number, 'barcode': barcode,
                    'errors': {'barcode': 'This barcode is already in use.'},
                })
                continue

            create_fields = {'library': library, 'book': book, 'barcode': barcode}
            for column in self.OPTIONAL_COLUMNS:
                if column in data:
                    create_fields[column] = data[column]

            # bulk_create skips Model.save(), so default acquired_at here.
            create_fields.setdefault('acquired_at', timezone.now())

            to_create.append(BookCopy(**create_fields))

        # ------------------------------------------------------------------
        # PHASE 5: Write everything inside ONE transaction.
        # ------------------------------------------------------------------
        created_count = 0
        updated_count = 0

        if to_create or to_update:
            involved_library_pks = sorted(
                {copy.library_id for copy in to_create}
                | {copy.library_id for copy, _touched in to_update}
            )
            try:
                with transaction.atomic():
                    # Lock the involved Library rows in a fixed order
                    # (sorted by pk -> no deadlocks). no_key=True is the
                    # lighter "FOR NO KEY UPDATE" lock: concurrent imports
                    # into the same library still queue up, but unrelated
                    # inserts that reference the library aren't blocked
                    # (see BookCopyCreateView for the full explanation).
                    list(
                        Library.objects
                        .select_for_update(no_key=True)
                        .filter(pk__in=involved_library_pks)
                        .order_by('pk')
                    )

                    if to_create:
                        BookCopy.objects.bulk_create(to_create)
                        created_count = len(to_create)

                    if to_update:
                        # Grouped by which fields each row actually
                        # changed, so an untouched field is never
                        # written back — see bulk.py.
                        bulk_update_grouped_by_fields(BookCopy, to_update)
                        updated_count = len(to_update)

            except IntegrityError:
                # Someone else created one of these barcodes between our
                # check in phase 2 and our INSERT. The transaction rolled
                # back, so nothing was saved and a retry is safe.
                return Response(
                    {
                        'detail': (
                            'The import conflicted with a concurrent change. '
                            'Nothing was saved; please retry.'
                        )
                    },
                    status=status.HTTP_409_CONFLICT,
                )

            # ---- Sync inventory. Outside the transaction above for the
            # same reason as the other views: each sync_inventory() call
            # manages its own short transaction and shouldn't extend how
            # long the Library-row locks above are held.
            #
            # Every created copy changes a count (a brand-new copy is
            # always +1 to total and to whichever status it starts in),
            # so all of `to_create`'s pairs need a sync. An updated copy
            # only needs one if its `status` was among the columns this
            # CSV row actually touched.
            touched_pairs = {(copy.library_id, copy.book_id) for copy in to_create}
            touched_pairs |= {
                (copy.library_id, copy.book_id)
                for copy, touched_fields in to_update
                if 'status' in touched_fields
            }
            sync_inventory_many(touched_pairs)

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