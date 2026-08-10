"""
urls.py — borrow_transactions app routes

Maps URL paths to views.py. Kept per-app so each app owns its own routes.
"""

from django.urls import path

from .views import (
    BorrowTransactionListView,
    BorrowTransactionDetailView,
    BorrowTransactionUpdateView,
    BorrowTransactionBulkUpdateView,
    BorrowCheckoutView,
    BorrowBatchCheckoutView,
    BorrowReturnView,
    BorrowBatchReturnView,
    BorrowTransactionExportView,
    BorrowTransactionStatisticsView,
)

# Namespaces these URLs, e.g. reverse('borrow_transactions:list').
app_name = 'borrow_transactions'

urlpatterns = [
    path('', BorrowTransactionListView.as_view(), name='list'),

    # <uuid:pk> matches a UUID segment and passes it in as pk.
    path('<uuid:pk>/', BorrowTransactionDetailView.as_view(), name='detail'),

    # Static paths — don't collide with '<uuid:pk>/' above.
    path('checkout/', BorrowCheckoutView.as_view(), name='checkout'),
    path('batch-checkout/', BorrowBatchCheckoutView.as_view(), name='batch-checkout'),
    path('<uuid:pk>/return/', BorrowReturnView.as_view(), name='return'),
    path('batch-return/', BorrowBatchReturnView.as_view(), name='batch-return'),
    path('<uuid:pk>/update/', BorrowTransactionUpdateView.as_view(), name='update'),
    path('bulk-update/', BorrowTransactionBulkUpdateView.as_view(), name='bulk-update'),

    path('export/', BorrowTransactionExportView.as_view(), name='export'),
    path('stats/', BorrowTransactionStatisticsView.as_view(), name='statistics'),
]

# --------------------------------------------------------------------
# Include this in the project-level urls.py:
#
#   from django.urls import path, include
#   urlpatterns = [
#       ...
#       path('api/borrow-transactions/', include('borrow_transactions.urls')),
#   ]
#
# Resulting endpoints:
#
#   GET  /api/borrow-transactions/                    -> list (paginated)
#   GET  /api/borrow-transactions/?status=active
#   GET  /api/borrow-transactions/?member=<uuid>&book_copy=<uuid>
#   GET  /api/borrow-transactions/?search=potter
#   GET  /api/borrow-transactions/?ordering=due_date | -due_date
#
#   GET  /api/borrow-transactions/<uuid>/              -> single transaction
#
#   PUT/PATCH /api/borrow-transactions/<uuid>/update/   -> librarian correction
#     (any of: book_copy, member, borrowed_at, due_date, returned_at,
#      status, fine_amount — BookCopy.status is synced automatically
#      based on the transaction's new status)
#
#   PATCH /api/borrow-transactions/bulk-update/         -> update many
#     (status/due_date/returned_at/fine_amount only; BookCopy.status
#     synced per row, same as above)
#     Body: [{"id": "<uuid>", "status": "returned"}, ...]
#
#   POST /api/borrow-transactions/checkout/
#     {"book_copy": "<uuid>", "member": "<uuid>"}
#
#   POST /api/borrow-transactions/batch-checkout/     -> checkout several copies, one member
#     {"member": "<uuid>", "book_copies": ["<uuid>", "<uuid>", ...]}
#     All-or-nothing: any unavailable/missing copy fails the WHOLE batch.
#
#   POST /api/borrow-transactions/<uuid>/return/
#     {}  or  {"condition": "good"}  or  {"returned_at": "..."}
#
#   POST /api/borrow-transactions/batch-return/       -> return several transactions at once
#     {"returned_at": "...", "transactions": [{"id": "<uuid>", "condition": "good"}, ...]}
#     ("returned_at" and each item's "condition" are optional.)
#     All-or-nothing: any missing/already-returned transaction fails the WHOLE batch.
#
#   GET  /api/borrow-transactions/stats/                -> aggregate counts
#
#   GET  /api/borrow-transactions/export/?format=csv|json  -> download (supports
#         the same search/filter/ordering params as the list endpoint)