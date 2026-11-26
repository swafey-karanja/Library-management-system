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

from core.permissions import (
    IsAdminOrLibrarian,
    LibraryScopedQuerysetMixin,
    get_user_library_id,
    scope_queryset_to_library,
)
from apps.members.models import Status as MemberStatus
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
from apps.reservations.models import Reservation
from apps.reservations.services import assign_or_release_book_copy
from apps.inventory.services import sync_inventory_many, sync_inventory_pairs
from apps.book_copies.services import set_copy_status
from rest_framework.exceptions import NotFound                       # next to the other rest_framework imports
from .corrections import COPY_RELEASE, CorrectionError, plan_correction   # next to `from .models import BorrowTransaction`

# ...and this constant, right after BORROW_TRANSACTION_LIBRARY_LOOKUP:
# Most corrections one bulk-update request may carry. Lower than the
# book_copies bulk limit because every item here also locks, and may
# change, a copy and its inventory (roughly 7 statements per item).
MAX_BULK_UPDATE_SIZE = 200

# ORM path from BorrowTransaction down to a library id. This model has
# no direct `library` FK (see models.py) — it's scoped through the
# copy it references instead, since the copy is what actually belongs
# to a library. Used everywhere below instead of repeating the string.
BORROW_TRANSACTION_LIBRARY_LOOKUP = 'book_copy__library_id'


class BorrowTransactionPagination(PageNumberPagination):
    page_size = 20
    page_size_query_param = 'page_size'
    max_page_size = 100


def _apply_copy_action(book_copy, action, touched):
    """
    Carries out the copy change that plan_correction() decided on (see
    corrections.py for the table of rules). `action` is None (leave the
    copy alone), COPY_RELEASE, or the status to set the copy to.

    This replaces the old _sync_book_copy_status(), which re-derived the
    copy's status from the loan's status on its own, from a copy read
    without a lock and with no check of what state the copy was in.
    Now the decision is made once, from the locked rows, in
    plan_correction(); this function only performs it.

    `touched` is a set the caller keeps for the whole request. Every
    (library, book) whose copy changed is added to it, and the caller
    syncs inventory for them AFTER its transaction.atomic() block has
    committed (sync_inventory_pairs) — never in here, where the
    inventory row lock would be held until the whole block commits.
    """
    if action is None:
        return
    touched.add((book_copy.library_id, book_copy.book_id))
    if action == COPY_RELEASE:
        # Gives the copy to the oldest waiting reservation, or puts it on
        # the shelf.
        assign_or_release_book_copy(book_copy, sync=False)
    else:
        set_copy_status(book_copy, action, sync=False)


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
    """
    How many loans this member currently has OUT: not returned, and not
    lost. A loan logged as lost stops counting the moment it is logged
    (it still has no returned_at, which is why returned_at alone is not
    enough). The member now owes for the book, not holds it.
    """
    return (
        BorrowTransaction.objects
        .filter(member=member, returned_at__isnull=True)
        .exclude(status=BorrowTransaction.STATUS_LOST)
        .count()
    )


def _lock_member_and_check_limit(member, user, new_loans):
    """
    Locks the member's row, THEN re-checks the member and counts their
    loans. Returns (fresh_member, None) when `new_loans` more loans fit,
    or (None, error Response). Call it INSIDE transaction.atomic(), as
    the first thing, before the book copies are locked.

    WHY THE LOCK
    ------------
    The old code counted the member's loans before opening the
    transaction. Two checkouts for the same member at the same moment
    both counted "4 of 5", both passed, and the member ended up with 6.
    There is no single loan row to lock for "how many does this member
    have", so we lock the one row that IS shared by all of that member's
    checkouts: the member row. The second request waits here, and when it
    gets the lock it counts again and sees the first one's loan.

    Details that matter:
      - of=('self',) locks only the member row, not the library row that
        select_related('library') also reads.
      - no_key=True (FOR NO KEY UPDATE) does not block the loan INSERT's
        own foreign-key check on this member row, so we don't deadlock
        with ourselves. It is also the weaker lock.
      - The member is re-read here, so a member deactivated a moment ago
        is refused too (status is re-checked on the fresh row).
      - Lock order everywhere is: member -> book copies -> reservations.
    """
    MemberModel = member.__class__
    try:
        fresh = (
            MemberModel.objects.select_related('library')
            .select_for_update(of=('self',), no_key=True)
            .get(pk=member.pk)
        )
    except MemberModel.DoesNotExist:
        return None, Response({'detail': 'Member not found.'}, status=status.HTTP_404_NOT_FOUND)

    member_error = _member_checkout_error(fresh, user)
    if member_error is not None:
        return None, member_error

    active_loan_count = _count_active_loans(fresh)
    loan_limit = _get_active_loan_limit(fresh.library)
    if active_loan_count + new_loans > loan_limit:
        if new_loans == 1:
            detail = f'This member has reached their active loan limit ({loan_limit}).'
        else:
            detail = (
                f'This member has {active_loan_count} active loan(s) and a limit of '
                f'{loan_limit}. This request would add {new_loans} more, which exceeds the limit.'
            )
        return None, Response({'detail': detail}, status=status.HTTP_409_CONFLICT)
    return fresh, None


def _member_checkout_error(member, user):
    """
    Checks the MEMBER half of a checkout request. Returns a ready-made
    error Response if this member can't be lent to by `user`, or None
    if the checkout may carry on. Shared by the single and batch
    checkout views so the two can't drift apart.

    Why this exists: the checkout serializers look the member up in the
    WHOLE members table (any library's member is accepted), and the
    views only ever scoped the book COPY. So a librarian could lend
    their own library's copy to a member of a different library. Two
    rules close that, plus the "active members only" business rule:

      1. A non-admin user may only lend to members of THEIR OWN library.
         Another library's member gets the same 404 as an id that
         doesn't exist, so the response never confirms that the id is
         real. (An admin has no library, so this rule skips them.)
      2. The member must be active. New members start 'inactive' until
         they confirm their email, so they can't borrow before that.

    A third rule — the member and the copy must belong to the SAME
    library — needs the copy, so the views check it once the copy has
    been fetched (that check also covers admins).
    """
    user_library_id = get_user_library_id(user)  # None for an admin
    if user_library_id is not None and member.library_id != user_library_id:
        return Response({'detail': 'Member not found.'}, status=status.HTTP_404_NOT_FOUND)

    if member.status != MemberStatus.ACTIVE:
        return Response(
            {'detail': 'This member is not active and cannot borrow books.'},
            status=status.HTTP_409_CONFLICT,
        )

    return None

def _check_checkout_holds(member, copies):
    """
    Decides, for each locked copy, whether it may be checked out to
    `member`, and which holds the checkout will use up.

    A copy on the shelf ('available') can go to anyone. A copy that is
    'reserved' is being kept for ONE member: it may only go to that member,
    and only while the hold has not run out. Any other status is refused.

    Call this INSIDE transaction.atomic(), after the copies are locked.
    The matching reservation rows are locked here too (FOR NO KEY UPDATE),
    so a cancel/expire running at the same moment has to wait for us and
    we never check out a copy whose hold was just cancelled.

    Returns (problems, hold_ids):
      problems — {copy id: message} for every copy that cannot be lent
      hold_ids — ids of the reservations to close once the loans exist
    """
    problems = {}
    reserved_ids = [c.pk for c in copies if c.status == c.STATUS_RESERVED]

    # ONE query for every held copy in the request (not one per copy).
    holds = {
        r.book_copy_id: r
        for r in Reservation.objects.select_for_update(of=('self',), no_key=True)
        .filter(book_copy_id__in=reserved_ids, status=Reservation.STATUS_RESERVED)
        .order_by('pk')
    } if reserved_ids else {}

    now = timezone.now()
    hold_ids = []
    for copy in copies:
        if copy.status == copy.STATUS_AVAILABLE:
            continue
        if copy.status != copy.STATUS_RESERVED:
            problems[copy.pk] = f'This copy is not available to borrow (current status: {copy.status}).'
            continue
        hold = holds.get(copy.pk)
        if hold is None:
            problems[copy.pk] = 'This copy is marked reserved but has no active hold.'
        elif hold.member_id != member.pk:
            problems[copy.pk] = 'This copy is being held for a different member.'
        elif hold.expires_at is not None and hold.expires_at <= now:
            problems[copy.pk] = 'The hold on this copy has expired.'
        else:
            hold_ids.append(hold.pk)
    return problems, hold_ids


def _close_holds(hold_ids):
    """Marks the holds that were just picked up as checked_out (one UPDATE)."""
    if hold_ids:
        Reservation.objects.filter(pk__in=hold_ids).update(status=Reservation.STATUS_CHECKED_OUT)


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
    status, waiving/adjusting a fine, etc. What a status change means for
    the loan's copy is decided by plan_correction() in corrections.py.

    HOW THIS VIEW AVOIDS LOST UPDATES
    ---------------------------------
    DRF's normal flow is: get_object() reads the row -> validate ->
    serializer.save() writes EVERY column back. If a return (or
    anything else) changes the row between that read and that write, the
    write silently puts the old values back — a returned loan becomes
    "active" again and its copy is flagged borrowed while sitting on the
    shelf.

    So perform_update() below does not trust the row get_object() read.
    Inside a transaction it:
      1. re-reads the loan and its copy WITH A LOCK (SELECT ... FOR NO
         KEY UPDATE) — any concurrent change is either already committed
         (and so visible here) or has to wait for us;
      2. decides what the correction means against that fresh state;
      3. writes ONLY the columns the request actually changed.
    get_object() is still used first, for the 404 / library scoping.
    """
    permission_classes = [IsAdminOrLibrarian]
    queryset = BorrowTransaction.objects.all()
    serializer_class = BorrowTransactionUpdateSerializer
    lookup_field = 'pk'
    library_lookup = BORROW_TRANSACTION_LIBRARY_LOOKUP

    def perform_update(self, serializer):
        # Only the fields the request actually sent (a PATCH body, or a
        # PUT body minus the read-only fields).
        changes = dict(serializer.validated_data)
        touched = set()   # (library, book) pairs whose inventory needs a sync

        with transaction.atomic():
            # of=('self', 'book_copy') locks the loan row and its copy
            # row and nothing else; no_key=True is the weaker lock that
            # doesn't block unrelated inserts referencing these rows
            # (same reasoning as BorrowReturnView).
            try:
                borrow_transaction = scope_queryset_to_library(
                    BorrowTransaction.objects
                    .select_related('book_copy')
                    .select_for_update(of=('self', 'book_copy'), no_key=True),
                    self.request.user,
                    library_lookup=self.library_lookup,
                ).get(pk=serializer.instance.pk)
            except BorrowTransaction.DoesNotExist:
                raise NotFound('Borrow transaction not found.')

            # The "before" snapshot for the notification now comes from
            # the locked, fresh row (not the stale one get_object() read).
            before = {field: getattr(borrow_transaction, field) for field in NOTIFIABLE_FIELDS}

            try:
                plan = plan_correction(borrow_transaction, borrow_transaction.book_copy, changes)
            except CorrectionError as exc:
                raise exc.as_api_exception()   # nothing has been written yet

            if plan.fields:
                for field_name, value in plan.fields.items():
                    setattr(borrow_transaction, field_name, value)
                # update_fields => the UPDATE names only these columns.
                borrow_transaction.save(update_fields=list(plan.fields))
            _apply_copy_action(borrow_transaction.book_copy, plan.copy_action, touched)

        # After the commit: inventory sync as its own short transaction.
        sync_inventory_pairs(touched)

        after = {field: getattr(borrow_transaction, field) for field in NOTIFIABLE_FIELDS}

        # DRF builds the response from serializer.instance, so point it at
        # the fresh object (the one get_object() read is out of date).
        serializer.instance = borrow_transaction
        notify_update(borrow_transaction.id, before, after)


class BorrowTransactionBulkUpdateView(APIView):
    """
    PATCH /api/borrow-transactions/bulk-update/

    Updates many transactions at once, restricted to status/due_date/
    returned_at/fine_amount. APIView (not a generic view) since the
    request body is a LIST of {id, ...fields} rather than one <pk>.

    Body: [{"id": "<uuid>", "status": "returned"}, ...]
    At most MAX_BULK_UPDATE_SIZE items. All-or-nothing: if any item can't
    be applied, nothing is changed and the response names the item.

    Each item follows exactly the same rules as the single update view
    (plan_correction() in corrections.py), and the same locking rule:
    LOCK the rows, then read their state, then write.

    DEADLOCKS
    ---------
    Two requests that touch the same rows in opposite orders can each
    wait for a row the other holds — Postgres then aborts one of them.
    To make that impossible, every request takes its locks in the same
    order:
      - the loans (and their copies) are all locked in ONE query,
        ORDER BY id;
      - items are then processed sorted by (library, book, loan id), so
        the per-title inventory rows are locked in one fixed order too
        (never in the order the client happened to list them).
    """
    permission_classes = [IsAdminOrLibrarian]

    def patch(self, request, *args, **kwargs):
        # Cheap check first: refuse an oversized body before doing any work.
        if isinstance(request.data, list) and len(request.data) > MAX_BULK_UPDATE_SIZE:
            return Response(
                {'detail': f'At most {MAX_BULK_UPDATE_SIZE} transactions can be updated per request.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

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
        items_by_id = {item['id']: item for item in items}
        touched = set()   # (library, book) pairs whose inventory needs a sync

        try:
            # atomic() = all-or-nothing. Raising inside it (CorrectionError
            # below) rolls back every change made so far.
            with transaction.atomic():
                # SCOPING: plain APIView, so scope_queryset_to_library() is
                # called directly. A loan outside the caller's library is
                # simply absent here and lands in "missing_ids" below.
                #
                # select_related pulls the copy's book and the member in the
                # SAME query so building the response needs no further
                # queries; of=('self', 'book_copy') keeps the lock to the
                # loan and copy rows, so the shared `books` catalogue row
                # (used by every library) is never locked.
                loans = list(
                    scope_queryset_to_library(
                        BorrowTransaction.objects
                        .select_related('book_copy__book', 'member')
                        .select_for_update(of=('self', 'book_copy'), no_key=True),
                        request.user,
                        library_lookup=BORROW_TRANSACTION_LIBRARY_LOOKUP,
                    ).filter(id__in=requested_ids).order_by('pk')
                )
                loans_by_id = {loan.id: loan for loan in loans}

                missing_ids = set(requested_ids) - set(loans_by_id)
                if missing_ids:
                    return Response(
                        {
                            'detail': 'One or more borrow transactions were not found.',
                            'missing_ids': [str(missing_id) for missing_id in missing_ids],
                        },
                        status=status.HTTP_400_BAD_REQUEST,
                    )

                # Two loans in this request can point at the SAME copy (an
                # old returned loan and a current one). select_related gave
                # each its own Python object for that copy, so a status the
                # first item sets would be invisible to the second. Make
                # them share one object.
                copies_by_id = {}
                for loan in loans:
                    loan.book_copy = copies_by_id.setdefault(loan.book_copy_id, loan.book_copy)

                # Fixed processing order => fixed lock order (see docstring).
                ordered_loans = sorted(
                    loans,
                    key=lambda loan: (str(loan.book_copy.library_id), str(loan.book_copy.book_id), str(loan.id)),
                )

                for loan in ordered_loans:
                    item = items_by_id[loan.id]
                    before = {field: getattr(loan, field) for field in NOTIFIABLE_FIELDS}

                    # Only fields actually present in this item.
                    changes = {name: item[name] for name in BULK_UPDATABLE_FIELDS if name in item}
                    try:
                        plan = plan_correction(loan, loan.book_copy, changes)
                    except CorrectionError as exc:
                        exc.loan_id = loan.id
                        raise

                    if plan.fields:
                        for field_name, value in plan.fields.items():
                            setattr(loan, field_name, value)
                        # update_fields => only these columns are written.
                        loan.save(update_fields=list(plan.fields))
                    _apply_copy_action(loan.book_copy, plan.copy_action, touched)

                    after = {field: getattr(loan, field) for field in NOTIFIABLE_FIELDS}
                    notify_update(loan.id, before, after)   # sent only if the whole batch commits
        except CorrectionError as exc:
            return Response(
                {'detail': exc.detail, 'id': str(exc.loan_id)},
                status=exc.status_code,
            )

        # The batch has committed. Sync each touched title once, in sorted
        # order, each as its own short transaction.
        sync_inventory_pairs(touched)

        # Respond in the order the client sent the items, not our sorted order.
        response_serializer = BorrowTransactionSerializer(
            [loans_by_id[loan_id] for loan_id in requested_ids], many=True
        )
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

        # Who may borrow: right library, and an active member.
        member_error = _member_checkout_error(member, request.user)
        if member_error is not None:
            return member_error

        touched = set()   # (library, book) pairs whose inventory needs a sync

        with transaction.atomic():
            # Lock the member and check their loan limit FIRST (see
            # _lock_member_and_check_limit for why it is locked).
            member, limit_error = _lock_member_and_check_limit(member, request.user, 1)
            if limit_error is not None:
                return limit_error

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

            # The copy and the member must belong to the same library.
            # For a librarian this can't fail any more (both were already
            # restricted to their library above), but an ADMIN is not
            # restricted to one library, so without this check an admin
            # could pair library A's copy with library B's member.
            if book_copy.library_id != member.library_id:
                return Response(
                    {'detail': 'The member and the book copy must belong to the same library.'},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            # On the shelf, or held for THIS member (see _check_checkout_holds).
            problems, hold_ids = _check_checkout_holds(member, [book_copy])
            if problems:
                return Response({'detail': problems[book_copy.pk]}, status=status.HTTP_409_CONFLICT)

            borrow_transaction = BorrowTransaction(
                book_copy=book_copy,
                member=member,
                borrowed_at=data.get('borrowed_at'),  # None -> model save() fills it in
                due_date=data.get('due_date'),
            )
            borrow_transaction.save()

            # sync=False: inventory is synced AFTER the commit (below), so
            # its row lock isn't held for the rest of this transaction.
            set_copy_status(book_copy, book_copy.STATUS_BORROWED, sync=False)
            touched.add((book_copy.library_id, book_copy.book_id))

            _close_holds(hold_ids)   # a picked-up hold becomes checked_out

            notify_checkout([borrow_transaction.id])

        sync_inventory_pairs(touched)

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

        # Who may borrow: right library, and an active member.
        member_error = _member_checkout_error(member, request.user)
        if member_error is not None:
            return member_error

        with transaction.atomic():
            # Lock the member and check their loan limit FIRST. The limit
            # counts existing loans PLUS everything in THIS batch (see
            # _lock_member_and_check_limit).
            member, limit_error = _lock_member_and_check_limit(member, request.user, len(book_copy_ids))
            if limit_error is not None:
                return limit_error

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
            problems, hold_ids = _check_checkout_holds(member, locked_copies)
            for copy_id in book_copy_ids:
                copy = locked_by_id.get(copy_id)
                if copy is None:
                    errors.append({'book_copy': str(copy_id), 'error': 'Not found.'})
                elif copy.library_id != member.library_id:
                    # Only reachable by an admin (a librarian's copies are
                    # already limited to their own library, and so is the
                    # member) — same rule as the single checkout view.
                    errors.append({
                        'book_copy': str(copy_id),
                        'barcode': copy.barcode,
                        'error': 'Belongs to a different library than the member.',
                    })
                elif copy_id in problems:
                    errors.append({
                        'book_copy': str(copy_id),
                        'barcode': copy.barcode,
                        'error': problems[copy_id],
                    })

            if errors:
                # Returning here exits the `with` block normally — no
                # writes have happened yet, so there's nothing to roll
                # back; the transaction just commits empty.
                return Response(
                    {
                        'detail': 'One or more copies cannot be checked out. No transactions were created.',
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

                set_copy_status(copy, copy.STATUS_BORROWED, sync=False)

                created_transactions.append(borrow_transaction)

            # INVENTORY SYNC: once per DISTINCT (library, book) pair in
            # this batch — a stack of copies scanned at the desk is
            # often several DIFFERENT titles, but any repeats still
            # collapse to one sync call each.
            _close_holds(hold_ids)   # one UPDATE for every picked-up hold

            touched_pairs = {
                (copy.library_id, copy.book_id) for copy in locked_copies
            }

        # After the commit, sorted, one short transaction per title.
        sync_inventory_pairs(touched_pairs)

        notify_checkout([txn.id for txn in created_transactions])

        output_serializer = BorrowTransactionSerializer(created_transactions, many=True)
        return Response(output_serializer.data, status=status.HTTP_201_CREATED)


class BorrowReturnView(APIView):
    """
    POST /api/borrow-transactions/<pk>/return/
    Body (optional): {"returned_at": "...", "condition": "good"}

    Marks the loan returned, calculates any fine, and puts the copy
    back on the shelf (or into "damaged" if a condition was given).

    CONCURRENCY (why the code below is ordered the way it is)
    ---------------------------------------------------------
    Two requests can arrive for the SAME loan at nearly the same moment
    (a double scan, or a browser retry after a slow response). The rule
    that keeps that safe is:

        LOCK the rows  ->  THEN check their state  ->  THEN write.

    An earlier version read the loan with a plain query and checked
    `returned_at` BEFORE opening the transaction. Both requests could
    pass that check, because neither had written anything yet. Both
    then "returned" the loan, and — worse — both called
    assign_or_release_book_copy(): the first handed the copy to the
    oldest waiting reservation, the second saw that reservation was no
    longer waiting and handed the SAME physical copy to the next one.

    Now the loan (and its copy) are locked with SELECT ... FOR NO KEY
    UPDATE inside the atomic block. A second request for the same loan
    simply waits at the lock; once the first commits, the second reads
    the FRESH row, sees `returned_at` is already set, and gets a clean
    400 without touching anything.
    """
    permission_classes = [IsAdminOrLibrarian]

    def post(self, request, *args, **kwargs):
        # STEP 1 — validate the request body BEFORE taking any lock.
        # This is pure in-memory work (it never queries the database),
        # so there is no reason to make other requests wait while we do it.
        input_serializer = BorrowReturnSerializer(data=request.data)
        input_serializer.is_valid(raise_exception=True)
        data = input_serializer.validated_data

        touched = set()   # (library, book) pairs whose inventory needs a sync

        with transaction.atomic():
            # STEP 2 — fetch AND lock the loan together with its copy,
            # in one query.
            #
            # select_for_update(...) adds "FOR NO KEY UPDATE" to the SQL.
            # Postgres then makes any other transaction that wants to
            # lock or change these same rows WAIT until we commit or roll
            # back (that waiting is what serializes the two requests).
            #
            #  - of=('self', 'book_copy'): lock exactly the loan row
            #    ('self') and its copy row, and NOTHING else. Without
            #    `of`, Postgres locks every table in the join.
            #  - no_key=True: "FOR NO KEY UPDATE" instead of plain
            #    "FOR UPDATE". We only change non-key columns
            #    (status, returned_at, fine_amount, condition), and this
            #    weaker lock does not block unrelated inserts that merely
            #    REFERENCE these rows through a foreign key (for example a
            #    reservation being assigned to this copy).
            #
            # SCOPING: scope_queryset_to_library() is still the only
            # library gate here. A loan from another library is absent
            # from the scoped queryset, so .get() raises DoesNotExist ->
            # the same 404 as a genuinely unknown pk.
            try:
                borrow_transaction = scope_queryset_to_library(
                    BorrowTransaction.objects
                    .select_related('book_copy')
                    .select_for_update(of=('self', 'book_copy'), no_key=True),
                    request.user,
                    library_lookup=BORROW_TRANSACTION_LIBRARY_LOOKUP,
                ).get(pk=kwargs['pk'])
            except BorrowTransaction.DoesNotExist:
                return Response({'detail': 'Borrow transaction not found.'}, status=status.HTTP_404_NOT_FOUND)

            # STEP 3 — the authoritative state check, now that we hold the
            # lock. If a concurrent request returned this loan first, we
            # were blocked at the query above and `borrow_transaction`
            # already reflects its committed result. Nothing has been
            # written yet, so returning here just commits an empty
            # transaction.
            if borrow_transaction.returned_at is not None:
                return Response({'detail': 'This transaction has already been returned.'}, status=status.HTTP_400_BAD_REQUEST)

            # STEP 4 — the writes. Safe: nobody else can touch this loan
            # or this copy until the atomic block ends.
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

            touched.add((book_copy.library_id, book_copy.book_id))
            if condition == book_copy.CONDITION_DAMAGED:
                set_copy_status(book_copy, book_copy.STATUS_DAMAGED, sync=False)
            else:
                # Never call assign_or_release_book_copy for a damaged
                # copy — only reaches here when it's genuinely returnable.
                # sync=False: inventory is synced after the commit (below).
                assign_or_release_book_copy(book_copy, sync=False)

            # NOTE: there used to be a final
            #     book_copy.save(update_fields=['status', 'condition'])
            # here. It was redundant (set_copy_status() above has already
            # saved the status, and the condition was saved just before),
            # and it re-wrote both columns from an in-memory copy of the
            # row that had been read earlier — a way to silently overwrite
            # a concurrent change. Removed.

        sync_inventory_pairs(touched)
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

        touched = set()   # (library, book) pairs whose inventory needs a sync

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

            # ONE MEMBER PER BATCH. A batch return is "one person handing
            # back a stack of books", and the confirmation email goes to
            # one person. Nothing in the request body names the member, so
            # we check it here, from the loans we just locked (member_id is
            # already on the row: no extra query). Checked before anything
            # is written, so a mixed batch changes nothing.
            member_ids = {txn.member_id for txn in locked_transactions}
            if len(member_ids) > 1:
                return Response(
                    {
                        'detail': 'A batch return must be for one member. Nothing was changed.',
                        'errors': [
                            {'id': str(txn.pk), 'member': str(txn.member_id)}
                            for txn in locked_transactions
                        ],
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )

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

                touched.add((book_copy.library_id, book_copy.book_id))
                if condition == book_copy.CONDITION_DAMAGED:
                    set_copy_status(book_copy, book_copy.STATUS_DAMAGED, sync=False)
                else:
                    assign_or_release_book_copy(book_copy, sync=False)
                # (A final book_copy.save(update_fields=['status','condition'])
                # used to be here. Removed for the same reason as in
                # BorrowReturnView: it re-wrote two columns from an
                # in-memory copy and could undo a concurrent change.)

                returned_transactions.append(borrow_transaction)

        sync_inventory_pairs(touched)
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
        # SCOPING: this plain APIView has no get_queryset() for the
        # mixin to hook into, so the queryset is scoped by hand. Every
        # number below is derived from it, so a librarian only ever
        # counts their own library's loans (admins still see all).
        queryset = scope_queryset_to_library(
            BorrowTransaction.objects.all(),
            request.user,
            library_lookup=BORROW_TRANSACTION_LIBRARY_LOOKUP,
        )

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