"""
views.py — BorrowTransaction views

  1. BorrowTransactionListView         — GET  /api/borrow-transactions/
  2. BorrowTransactionDetailView       — GET  /api/borrow-transactions/<pk>/
  3. BorrowTransactionActiveByCopyView — GET  /api/borrow-transactions/active/<identifier>/
  4. BorrowTransactionUpdateView       — PUT/PATCH /api/borrow-transactions/<pk>/update/
  5. BorrowTransactionBulkUpdateView   — PATCH /api/borrow-transactions/bulk-update/
  6. BorrowCheckoutView                — POST /api/borrow-transactions/checkout/
  7. BorrowBatchCheckoutView           — POST /api/borrow-transactions/batch-checkout/
  8. BorrowReturnView                  — POST /api/borrow-transactions/<pk>/return/
  9. BorrowBatchReturnView             — POST /api/borrow-transactions/batch-return/
 10. BorrowTransactionExportView       — GET  /api/borrow-transactions/export/
 11. BorrowTransactionStatisticsView   — GET  /api/borrow-transactions/statistics/
"""

import csv
import uuid

from django.db import transaction
from django.db.models import Count, Q
from django.http import HttpResponse
from django.utils import timezone

from rest_framework import generics, status
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.pagination import PageNumberPagination
from rest_framework.filters import SearchFilter, OrderingFilter
from rest_framework.generics import get_object_or_404
from django_filters.rest_framework import DjangoFilterBackend

from core.permissions import IsAdminOrLibrarian, LibraryScopedQuerysetMixin, scope_queryset_to_library
from .models import BorrowTransaction
from .serializers import (
    BorrowTransactionSerializer,
    BorrowTransactionUpdateSerializer,
    BorrowTransactionBulkUpdateItemSerializer,
    BorrowCheckoutSerializer,
    BorrowReturnSerializer,
    BorrowBatchCheckoutSerializer,
    BorrowBatchReturnSerializer,
    BULK_UPDATABLE_FIELDS,
)
from .notifications import notify_checkout, notify_return, notify_update, NOTIFIABLE_FIELDS
from apps.reservations.services import assign_or_release_book_copy, close_matching_reservation

# ORM path from BorrowTransaction down to a library id. This model has
# no direct `library` FK (see models.py) — it's scoped through the
# copy it references instead, since the copy is what actually belongs
# to a library. Used everywhere below instead of repeating the string.
BORROW_TRANSACTION_LIBRARY_LOOKUP = 'book_copy__library_id'


class BorrowTransactionPagination(PageNumberPagination):
    page_size = 20
    page_size_query_param = 'page_size'
    max_page_size = 100


def _sync_book_copy_status(borrow_transaction):
    book_copy = borrow_transaction.book_copy

    if borrow_transaction.status == BorrowTransaction.STATUS_RETURNED:
        if book_copy.status != book_copy.STATUS_AVAILABLE:
            assign_or_release_book_copy(book_copy)
        return

    status_map = {
        BorrowTransaction.STATUS_ACTIVE: book_copy.STATUS_BORROWED,
        BorrowTransaction.STATUS_OVERDUE: book_copy.STATUS_BORROWED,
        BorrowTransaction.STATUS_LOST: book_copy.STATUS_LOST,
    }
    new_copy_status = status_map.get(borrow_transaction.status)
    if new_copy_status and book_copy.status != new_copy_status:
        book_copy.status = new_copy_status
        book_copy.save(update_fields=['status'])


# Fallback cap on active (not yet returned) loans a member may hold at
# once, used ONLY when a library hasn't configured its own value (see
# _get_active_loan_limit below). Kept as a constant so it's easy to
# find/tune, same reasoning as BULK_UPDATABLE_FIELDS in serializers.py.
DEFAULT_MAX_ACTIVE_LOANS_PER_MEMBER = 5

# The JSON key read from library.enabled_modules for a per-library
# override. Using the existing enabled_modules JSONB column means this
# is configurable per library WITHOUT a schema change — e.g.:
#   UPDATE library SET enabled_modules = enabled_modules ||
#       '{"max_active_loans": 10}' WHERE library_id = '...';
LIBRARY_MAX_ACTIVE_LOANS_KEY = 'max_active_loans'


def _get_active_loan_limit(library):
    """
    A library's own active-loan cap, if it's set one, else the global
    default. `library` may be None (e.g. if a member somehow has no
    library set) — falls back to the default in that case too.

    ASSUMPTION: this assumes the Member model has a `library` FK
    (mirroring BookCopy's `library` FK in the book_copies app) — check
    this against the actual members app once it exists.
    """
    if library is not None and isinstance(library.enabled_modules, dict):
        configured_limit = library.enabled_modules.get(LIBRARY_MAX_ACTIVE_LOANS_KEY)
        if isinstance(configured_limit, int) and configured_limit > 0:
            return configured_limit
    return DEFAULT_MAX_ACTIVE_LOANS_PER_MEMBER


def _count_active_loans(member):
    """How many not-yet-returned transactions this member currently has."""
    return BorrowTransaction.objects.filter(member=member, returned_at__isnull=True).count()


class BorrowTransactionListView(LibraryScopedQuerysetMixin, generics.ListAPIView):
    """
    GET /api/borrow-transactions/

    - ?status=active|returned|overdue|lost
    - ?member=<uuid>  ?book_copy=<uuid>
    - ?search=term    -> member name / book title / barcode
    - ?ordering=borrowed_at | -borrowed_at | due_date | -due_date
    """
    permission_classes = [IsAdminOrLibrarian]

    # select_related avoids N+1 queries for member_name / book_title / barcode.
    queryset = BorrowTransaction.objects.select_related('member', 'book_copy', 'book_copy__book').all()

    serializer_class = BorrowTransactionSerializer
    library_lookup = BORROW_TRANSACTION_LIBRARY_LOOKUP
    pagination_class = BorrowTransactionPagination

    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_fields = ['status', 'member', 'book_copy']
    search_fields = ['member__name', 'book_copy__book__title', 'book_copy__barcode']
    ordering_fields = ['borrowed_at', 'due_date', 'created_at']
    ordering = ['-borrowed_at']  # stable default so pagination doesn't shift between requests


class BorrowTransactionDetailView(LibraryScopedQuerysetMixin, generics.RetrieveAPIView):
    """GET /api/borrow-transactions/<pk>/"""
    permission_classes = [IsAdminOrLibrarian]
    queryset = BorrowTransaction.objects.select_related('member', 'book_copy', 'book_copy__book').all()
    serializer_class = BorrowTransactionSerializer
    lookup_field = 'pk'
    library_lookup = BORROW_TRANSACTION_LIBRARY_LOOKUP


class BorrowTransactionActiveByCopyView(LibraryScopedQuerysetMixin, generics.RetrieveAPIView):
    """
    GET /api/borrow-transactions/active/<identifier>/

    <identifier> is EITHER a BookCopy's UUID or its barcode — same dual
    lookup as BookCopyDetailView in the book_copies app. Returns that
    copy's currently active (not yet returned) transaction, if any.

    This is what a "return" screen calls right after a scan: scan ->
    GET this URL -> get the transaction id back -> POST it to
    <pk>/return/. It's the return-side equivalent of scanning a
    barcode straight into BookCopyDetailView for checkout — without
    it, returning a copy required a separate list-endpoint query
    (?book_copy=<uuid>&status=active) to find the right transaction.

    404 if the copy doesn't exist, OR exists but isn't currently
    checked out (nothing to return).
    """
    permission_classes = [IsAdminOrLibrarian]
    serializer_class = BorrowTransactionSerializer
    library_lookup = BORROW_TRANSACTION_LIBRARY_LOOKUP

    # BUG FIX: this view previously had no `queryset` attribute, and
    # get_object() below queried BorrowTransaction.objects directly
    # instead of going through self.get_queryset() — so the mixin's
    # scoping was never actually applied here, despite being declared
    # on the class. `queryset` here is what the mixin's get_queryset()
    # scopes; get_object() now builds on top of THAT instead.
    queryset = BorrowTransaction.objects.select_related(
        'member', 'book_copy', 'book_copy__book'
    ).filter(returned_at__isnull=True)

    def get_object(self):
        identifier = self.kwargs['identifier']

        # Same UUID-vs-barcode detection as BookCopyDetailView, just
        # filtering through the book_copy relation instead of the
        # BookCopy table directly.
        try:
            uuid.UUID(identifier)
            copy_lookup = Q(book_copy_id=identifier)
        except ValueError:
            copy_lookup = Q(book_copy__barcode=identifier)

        # self.get_queryset() -> mixin's SCOPED version -> narrowed to
        # this one copy's active loan. Same pattern as
        # BookCopyDetailView.get_object() in the book_copies app.
        queryset = self.filter_queryset(self.get_queryset()).filter(copy_lookup)

        # get_object_or_404 raises Http404 (-> DRF 404 response) if no
        # row matches — covers "copy doesn't exist", "copy isn't
        # checked out", AND "copy belongs to another library" with one
        # response, same as intended for every scoped 404 elsewhere.
        obj = get_object_or_404(queryset)
        self.check_object_permissions(self.request, obj)
        return obj


class BorrowTransactionUpdateView(LibraryScopedQuerysetMixin, generics.UpdateAPIView):
    """
    PUT (full) / PATCH (partial) /api/borrow-transactions/<pk>/update/

    For librarian corrections — fixing a wrong due_date, a mis-set
    status, waiving/adjusting a fine, etc. After saving, the linked
    BookCopy's status is synced to match (see _sync_book_copy_status).
    """
    permission_classes = [IsAdminOrLibrarian]
    queryset = BorrowTransaction.objects.all()
    serializer_class = BorrowTransactionUpdateSerializer
    lookup_field = 'pk'
    library_lookup = BORROW_TRANSACTION_LIBRARY_LOOKUP

    def perform_update(self, serializer):
        before = {field: getattr(serializer.instance, field) for field in NOTIFIABLE_FIELDS}

        with transaction.atomic():
            borrow_transaction = serializer.save()
            _sync_book_copy_status(borrow_transaction)

        after = {field: getattr(borrow_transaction, field) for field in NOTIFIABLE_FIELDS}
        notify_update(borrow_transaction.id, before, after)


class BorrowTransactionBulkUpdateView(APIView):
    """
    PATCH /api/borrow-transactions/bulk-update/

    Updates many transactions at once, restricted to status/due_date/
    returned_at/fine_amount. APIView (not a generic view) since the
    request body is a LIST of {id, ...fields} rather than one <pk>.

    Body: [{"id": "<uuid>", "status": "returned"}, ...]

    Each transaction's linked BookCopy is synced to match its new
    status, same as the single-item update view.
    """
    permission_classes = [IsAdminOrLibrarian]

    def patch(self, request, *args, **kwargs):
        item_serializer = BorrowTransactionBulkUpdateItemSerializer(data=request.data, many=True)
        item_serializer.is_valid(raise_exception=True)
        items = item_serializer.validated_data

        # Reject duplicate ids outright rather than guessing intent.
        requested_ids = [item['id'] for item in items]
        if len(requested_ids) != len(set(requested_ids)):
            return Response(
                {'detail': 'Duplicate id values found in the request body.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # SCOPING: plain APIView, no get_queryset() for the mixin to
        # hook into, so scope_queryset_to_library() is called directly
        # here. A transaction outside the caller's library is simply
        # absent from `existing_transactions` — so it falls straight
        # into the existing "missing_ids" check below, same error as a
        # genuinely bad id.
        existing_transactions = scope_queryset_to_library(
            BorrowTransaction.objects.select_related('book_copy'),
            request.user,
            library_lookup=BORROW_TRANSACTION_LIBRARY_LOOKUP,
        ).filter(id__in=requested_ids)
        transactions_by_id = {txn.id: txn for txn in existing_transactions}

        missing_ids = set(requested_ids) - set(transactions_by_id.keys())
        if missing_ids:
            return Response(
                {
                    'detail': 'One or more borrow transactions were not found.',
                    'missing_ids': [str(missing_id) for missing_id in missing_ids],
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        # atomic() = all-or-nothing if something goes wrong mid-batch.
        updated_transactions = []
        with transaction.atomic():
            for item in items:
                borrow_transaction = transactions_by_id[item['id']]
                before = {field: getattr(borrow_transaction, field) for field in NOTIFIABLE_FIELDS}

                # Only touch fields actually present in this item.
                changed_fields = [
                    field_name
                    for field_name in BULK_UPDATABLE_FIELDS
                    if field_name in item
                ]
                for field_name in changed_fields:
                    setattr(borrow_transaction, field_name, item[field_name])

                # update_fields restricts the SQL UPDATE to just these
                # columns — extra safety net beyond the serializer.
                borrow_transaction.save(update_fields=changed_fields)
                _sync_book_copy_status(borrow_transaction)

                after = {field: getattr(borrow_transaction, field) for field in NOTIFIABLE_FIELDS}
                notify_update(borrow_transaction.id, before, after)

                updated_transactions.append(borrow_transaction)

        response_serializer = BorrowTransactionSerializer(updated_transactions, many=True)
        return Response(response_serializer.data, status=status.HTTP_200_OK)


class BorrowCheckoutView(APIView):
    """
    POST /api/borrow-transactions/checkout/
    Body: {"book_copy": "<uuid>", "member": "<uuid>"}

    Creates the loan record AND flips the copy's status to checked_out —
    both happen or neither does (transaction.atomic).
    """
    permission_classes = [IsAdminOrLibrarian]

    def post(self, request, *args, **kwargs):
        input_serializer = BorrowCheckoutSerializer(data=request.data)
        input_serializer.is_valid(raise_exception=True)
        data = input_serializer.validated_data

        book_copy = data['book_copy']
        member = data['member']
        BookCopyModel = book_copy.__class__

        # Loan-limit check. Deliberately a plain count, not locked with
        # select_for_update — a member's "how many books do I have out"
        # isn't a single row we can lock the way a specific BookCopy
        # is below, so two simultaneous checkouts for the same member
        # could in rare cases both pass this check. Low-stakes enough
        # (unlike double-lending one physical copy) to accept as a
        # best-effort check rather than adding lock complexity for it.
        active_loan_count = _count_active_loans(member)
        loan_limit = _get_active_loan_limit(member.library)
        if active_loan_count + 1 > loan_limit:
            return Response(
                {'detail': f'This member has reached their active loan limit ({loan_limit}).'},
                status=status.HTTP_409_CONFLICT,
            )

        with transaction.atomic():
            # SCOPING: BorrowCheckoutSerializer's own queryset for
            # `book_copy` is unscoped (any library's copy passes that
            # first check) — this re-fetch is the real gate. Scoped +
            # select_for_update together: a librarian can't lock/check
            # out another library's copy, and two simultaneous
            # checkouts of the same copy can't both pass "is it
            # available" either.
            try:
                book_copy = scope_queryset_to_library(
                    BookCopyModel.objects.select_for_update(), request.user
                ).get(pk=book_copy.pk)
            except BookCopyModel.DoesNotExist:
                return Response({'detail': 'Book copy not found.'}, status=status.HTTP_404_NOT_FOUND)

            if book_copy.status != book_copy.STATUS_AVAILABLE:
                return Response(
                    {'detail': f'This copy is not available to borrow (current status: {book_copy.status}).'},
                    status=status.HTTP_409_CONFLICT,
                )

            borrow_transaction = BorrowTransaction(
                book_copy=book_copy,
                member=member,
                borrowed_at=data.get('borrowed_at'),  # None -> model save() fills it in
                due_date=data.get('due_date'),
            )
            borrow_transaction.save()

            book_copy.status = book_copy.STATUS_BORROWED
            book_copy.save(update_fields=['status'])

            close_matching_reservation(member, book_copy)  # NEW

            notify_checkout([borrow_transaction.id])

        output_serializer = BorrowTransactionSerializer(borrow_transaction)
        return Response(output_serializer.data, status=status.HTTP_201_CREATED)


class BorrowBatchCheckoutView(APIView):
    """
    POST /api/borrow-transactions/batch-checkout/
    Body: {"member": "<uuid>", "book_copies": ["<uuid>", "<uuid>", ...]}

    Checks out several copies to ONE member in a single request — e.g.
    a librarian scanning a stack of books at the desk.

    ALL-OR-NOTHING: if any listed copy is missing or not available,
    NO transactions are created and NO copy statuses change. This is
    one transaction.atomic() block covering the whole batch, same
    pattern as the single-item BorrowCheckoutView above, just looping
    over several copies instead of one.
    """
    permission_classes = [IsAdminOrLibrarian]

    def post(self, request, *args, **kwargs):
        input_serializer = BorrowBatchCheckoutSerializer(data=request.data)
        input_serializer.is_valid(raise_exception=True)
        data = input_serializer.validated_data

        member = data['member']
        book_copies = data['book_copies']
        book_copy_ids = [copy.pk for copy in book_copies]
        # Model class, resolved dynamically (not imported directly) —
        # same trick used in serializers.py, avoids a circular import
        # between the borrow_transactions and book_copies apps.
        BookCopyModel = book_copies[0].__class__

        # Loan-limit check: existing active loans PLUS everything in
        # THIS batch, since a member could otherwise check out several
        # books in one request and blow past their limit in one shot,
        # even though each individual request "looked" under the cap.
        # Same best-effort reasoning as the single-checkout view — see
        # comment there.
        active_loan_count = _count_active_loans(member)
        loan_limit = _get_active_loan_limit(member.library)
        if active_loan_count + len(book_copy_ids) > loan_limit:
            return Response(
                {
                    'detail': (
                        f'This member has {active_loan_count} active loan(s) and a limit of '
                        f'{loan_limit}. This request would add {len(book_copy_ids)} more, '
                        f'which exceeds the limit.'
                    )
                },
                status=status.HTTP_409_CONFLICT,
            )

        with transaction.atomic():
            # SCOPING: same reasoning as BorrowCheckoutView — the
            # serializer's own PrimaryKeyRelatedField queryset is
            # unscoped, so this is the real gate. A copy outside the
            # caller's library is simply absent from `locked_copies`,
            # which the existing "Not found" branch below already
            # handles — no extra error-handling code needed here.
            locked_copies = list(
                scope_queryset_to_library(BookCopyModel.objects.select_for_update(), request.user)
                .filter(pk__in=book_copy_ids)
                .order_by('pk')
            )
            locked_by_id = {copy.pk: copy for copy in locked_copies}

            # Authoritative check, now that we hold the locks. Collects
            # EVERY problem rather than stopping at the first, so the
            # librarian's screen can show exactly which scans failed.
            errors = []
            for copy_id in book_copy_ids:
                copy = locked_by_id.get(copy_id)
                if copy is None:
                    errors.append({'book_copy': str(copy_id), 'error': 'Not found.'})
                elif copy.status != copy.STATUS_AVAILABLE:
                    errors.append({
                        'book_copy': str(copy_id),
                        'barcode': copy.barcode,
                        'error': f'Not available (status: {copy.status}).',
                    })

            if errors:
                # Returning here exits the `with` block normally — no
                # writes have happened yet, so there's nothing to roll
                # back; the transaction just commits empty.
                return Response(
                    {
                        'detail': 'One or more copies are not available. No transactions were created.',
                        'errors': errors,
                    },
                    status=status.HTTP_409_CONFLICT,
                )

            created_transactions = []
            for copy_id in book_copy_ids:
                copy = locked_by_id[copy_id]
                borrow_transaction = BorrowTransaction(
                    book_copy=copy,
                    member=member,
                    borrowed_at=data.get('borrowed_at'),
                    due_date=data.get('due_date'),
                )
                borrow_transaction.save()

                copy.status = copy.STATUS_BORROWED
                copy.save(update_fields=['status'])

                close_matching_reservation(member, copy)

                created_transactions.append(borrow_transaction)

        notify_checkout([txn.id for txn in created_transactions])

        output_serializer = BorrowTransactionSerializer(created_transactions, many=True)
        return Response(output_serializer.data, status=status.HTTP_201_CREATED)


class BorrowReturnView(APIView):
    """
    POST /api/borrow-transactions/<pk>/return/
    Body (optional): {"returned_at": "...", "condition": "good"}

    Marks the loan returned, calculates any fine, and puts the copy
    back on the shelf (or into "damaged" if a condition was given).
    """
    permission_classes = [IsAdminOrLibrarian]

    def post(self, request, *args, **kwargs):
        # SCOPING: plain pk lookup by URL <pk>, no serializer queryset
        # to fall back on — scope_queryset_to_library() is the only
        # gate here. A transaction outside the caller's library is
        # absent from the scoped queryset, so .get() raises
        # DoesNotExist -> the SAME 404 already used for a bad pk below.
        try:
            borrow_transaction = scope_queryset_to_library(
                BorrowTransaction.objects.select_related('book_copy'),
                request.user,
                library_lookup=BORROW_TRANSACTION_LIBRARY_LOOKUP,
            ).get(pk=kwargs['pk'])
        except BorrowTransaction.DoesNotExist:
            return Response({'detail': 'Borrow transaction not found.'}, status=status.HTTP_404_NOT_FOUND)

        if borrow_transaction.returned_at is not None:
            return Response({'detail': 'This transaction has already been returned.'}, status=status.HTTP_400_BAD_REQUEST)

        input_serializer = BorrowReturnSerializer(data=request.data)
        input_serializer.is_valid(raise_exception=True)
        data = input_serializer.validated_data

        with transaction.atomic():
            borrow_transaction.returned_at = data.get('returned_at') or timezone.now()
            # Fine is computed off due_date vs returned_at, then locked in.
            borrow_transaction.fine_amount = borrow_transaction.calculate_fine()
            borrow_transaction.status = BorrowTransaction.STATUS_RETURNED
            borrow_transaction.save(update_fields=['returned_at', 'fine_amount', 'status'])

            book_copy = borrow_transaction.book_copy
            condition = data.get('condition')
            if condition:
                book_copy.condition = condition
                book_copy.save(update_fields=['condition'])

            if condition == book_copy.CONDITION_DAMAGED:
                book_copy.status = book_copy.STATUS_DAMAGED
                book_copy.save(update_fields=['status'])
            else:
                # Never call assign_or_release_book_copy for a damaged
                # copy — only reaches here when it's genuinely returnable.
                assign_or_release_book_copy(book_copy)
            book_copy.save(update_fields=['status', 'condition'])

        notify_return([borrow_transaction.id])

        output_serializer = BorrowTransactionSerializer(borrow_transaction)
        return Response(output_serializer.data, status=status.HTTP_200_OK)


class BorrowBatchReturnView(APIView):
    """
    POST /api/borrow-transactions/batch-return/
    Body: {"returned_at": "...", "transactions": [{"id": "<uuid>", "condition": "good"}, ...]}
    ("returned_at" and each item's "condition" are optional.)

    Returns several transactions at once — e.g. a librarian scanning a
    stack of books being handed back by one member.

    ALL-OR-NOTHING: if any listed transaction is missing or already
    returned, NONE are returned and NO copy statuses change — one
    transaction.atomic() block for the whole batch, same shape as
    BorrowBatchCheckoutView above.
    """
    permission_classes = [IsAdminOrLibrarian]

    def post(self, request, *args, **kwargs):
        input_serializer = BorrowBatchReturnSerializer(data=request.data)
        input_serializer.is_valid(raise_exception=True)
        data = input_serializer.validated_data

        items = data['transactions']
        shared_returned_at = data.get('returned_at')
        requested_ids = [item['id'] for item in items]

        with transaction.atomic():
            # select_for_update + select_related locks BOTH the
            # borrow_transactions rows AND their linked book_copies
            # rows in one query — Postgres locks joined tables too by
            # default. That's exactly what we want: nothing else can
            # check out or return these same copies until we commit.
            #
            # SCOPING: same pattern as bulk-update — a transaction
            # outside the caller's library is simply absent from
            # `locked_transactions`, so it falls into the existing
            # "Not found" branch below with no extra code needed.
            locked_transactions = list(
                scope_queryset_to_library(
                    BorrowTransaction.objects.select_for_update().select_related('book_copy'),
                    request.user,
                    library_lookup=BORROW_TRANSACTION_LIBRARY_LOOKUP,
                )
                .filter(pk__in=requested_ids)
                .order_by('pk')  # consistent lock order, same reasoning as batch checkout
            )
            transactions_by_id = {txn.pk: txn for txn in locked_transactions}

            # Authoritative check, now that we hold the locks.
            errors = []
            for item in items:
                borrow_transaction = transactions_by_id.get(item['id'])
                if borrow_transaction is None:
                    errors.append({'id': str(item['id']), 'error': 'Not found.'})
                elif borrow_transaction.returned_at is not None:
                    errors.append({'id': str(item['id']), 'error': 'Already returned.'})

            if errors:
                return Response(
                    {
                        'detail': 'One or more transactions could not be returned. Nothing was changed.',
                        'errors': errors,
                    },
                    status=status.HTTP_409_CONFLICT,
                )

            returned_transactions = []
            for item in items:
                borrow_transaction = transactions_by_id[item['id']]

                borrow_transaction.returned_at = shared_returned_at or timezone.now()
                borrow_transaction.fine_amount = borrow_transaction.calculate_fine()
                borrow_transaction.status = BorrowTransaction.STATUS_RETURNED
                borrow_transaction.save(update_fields=['returned_at', 'fine_amount', 'status'])

                book_copy = borrow_transaction.book_copy
                condition = item.get('condition')
                if condition:
                    book_copy.condition = condition
                    book_copy.save(update_fields=['condition'])

                if condition == book_copy.CONDITION_DAMAGED:
                    book_copy.status = book_copy.STATUS_DAMAGED
                    book_copy.save(update_fields=['status'])
                else:
                    assign_or_release_book_copy(book_copy)
                book_copy.save(update_fields=['status', 'condition'])

                returned_transactions.append(borrow_transaction)

        notify_return([txn.id for txn in returned_transactions])

        output_serializer = BorrowTransactionSerializer(returned_transactions, many=True)
        return Response(output_serializer.data, status=status.HTTP_200_OK)


class BorrowTransactionExportView(LibraryScopedQuerysetMixin, generics.GenericAPIView):
    """
    GET /api/borrow-transactions/export/?format=csv|json

    Reuses the list endpoint's filtering/search/ordering, e.g.:
        ?status=overdue&member=<uuid>&format=csv

    GenericAPIView (not ListAPIView/APIView) gives us filter_queryset()
    and get_serializer() without forcing the normal paginated JSON
    response shape, since this returns a file instead.
    """
    permission_classes = [IsAdminOrLibrarian]

    queryset = BorrowTransaction.objects.select_related('member', 'book_copy', 'book_copy__book').all()
    serializer_class = BorrowTransactionSerializer
    library_lookup = BORROW_TRANSACTION_LIBRARY_LOOKUP

    # Same filter/search/ordering config as BorrowTransactionListView,
    # so exports can be scoped with the exact params used to browse.
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_fields = ['status', 'member', 'book_copy']
    search_fields = ['member__name', 'book_copy__book__title', 'book_copy__barcode']
    ordering_fields = ['borrowed_at', 'due_date', 'created_at']
    ordering = ['-borrowed_at']

    # Explicit column set/order for the exported file, kept stable
    # even if the serializer's fields change later.
    EXPORT_FIELDS = [
        'id', 'member', 'member_name', 'book_copy', 'barcode', 'book_title',
        'borrowed_at', 'due_date', 'returned_at', 'status', 'fine_amount',
        'is_overdue', 'days_overdue', 'created_at',
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
        response['Content-Disposition'] = 'attachment; filename="borrow_transactions_export.csv"'

        # HttpResponse is file-like, so csv.writer can write straight into it.
        writer = csv.writer(response)
        writer.writerow(self.EXPORT_FIELDS)
        for row in serializer.data:
            writer.writerow([row.get(field, '') for field in self.EXPORT_FIELDS])

        return response

    def _export_json(self, queryset):
        serializer = self.get_serializer(queryset, many=True)

        response = HttpResponse(content_type='application/json')
        response['Content-Disposition'] = 'attachment; filename="borrow_transactions_export.json"'

        # JSONRenderer (not json.dumps) correctly handles the UUID/
        # datetime/Decimal types already present in serializer.data.
        from rest_framework.renderers import JSONRenderer
        response.write(JSONRenderer().render(serializer.data))
        return response


class BorrowTransactionStatisticsView(APIView):
    permission_classes = [IsAdminOrLibrarian]
    """
    GET /api/borrow-transactions/statistics/
    Counts/breakdowns, not individual rows.
    """

    def get(self, request, *args, **kwargs):
        queryset = BorrowTransaction.objects.all()

        # GROUP BY status, then fill in any status with zero rows so
        # the frontend doesn't have to guess "missing" vs "zero".
        status_breakdown = queryset.values('status').annotate(count=Count('id'))
        by_status = {row['status']: row['count'] for row in status_breakdown}
        by_status = {
            choice_value: by_status.get(choice_value, 0)
            for choice_value, _label in BorrowTransaction.STATUS_CHOICES
        }

        # Computed in Python (not the DB) since "overdue" depends on
        # comparing due_date to *now*, per active row.
        active_loans = queryset.filter(returned_at__isnull=True)
        overdue_count = sum(1 for loan in active_loans if loan.is_overdue)

        data = {
            'total_transactions': queryset.count(),
            'by_status': by_status,
            'currently_borrowed': active_loans.count(),
            'currently_overdue': overdue_count,
        }

        return Response(data)