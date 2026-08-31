from rest_framework import generics, filters, status
from rest_framework.views import APIView
from rest_framework.response import Response
from django_filters.rest_framework import DjangoFilterBackend
from django.db import transaction
from django.shortcuts import get_object_or_404

from core.permissions import IsAdminOrLibrarian, LibraryScopedQuerysetMixin, scope_queryset_to_library

from .models import Reservation
from .serializers import ReservationListSerializer, ReservationCreateSerializer
from .filters import ReservationFilter
from .pagination import ReservationPagination
from .notifications import notify_reservation_created, notify_reservation_cancelled
from .services import assign_or_release_book_copy

# Reservation has a DIRECT `library` FK (unlike BorrowTransaction, which
# has to reach through book_copy__library_id) — scoping uses the field
# directly, same pattern used for BookCopy elsewhere in the project.
RESERVATION_LIBRARY_LOOKUP = 'library_id'


class ReservationListCreateView(LibraryScopedQuerysetMixin, generics.ListCreateAPIView):
    """
    GET  /api/reservations/  -> list reservations (filterable, searchable, paginated)
    POST /api/reservations/  -> place a new reservation

    Staff-only (IsAdminOrLibrarian) and library-scoped, matching every
    borrow_transactions endpoint — a librarian only sees/creates
    reservations against their own library's stock.
    """
    permission_classes = [IsAdminOrLibrarian]
    library_lookup = RESERVATION_LIBRARY_LOOKUP

    queryset = Reservation.objects.select_related('book', 'library', 'book_copy', 'member').all()

    filter_backends = [DjangoFilterBackend, filters.SearchFilter]
    filterset_class = ReservationFilter
    search_fields = ['member__name', 'member__email']
    pagination_class = ReservationPagination

    def get_serializer_class(self):
        if self.request.method == 'POST':
            return ReservationCreateSerializer
        return ReservationListSerializer

    def perform_create(self, serializer):
        # `library` is NEVER taken from the request body — always the
        # requesting staff member's own library. See the flagged note
        # below on request.user.library.
        reservation = serializer.save(library=self.request.user.library)
        notify_reservation_created(reservation.id)


class ReservationCancelView(APIView):
    """
    POST /api/reservations/<uuid:pk>/cancel/

    Cancels a reservation that's still 'waiting' or 'reserved'. If a
    copy had already been claimed for it (status='reserved'), that
    copy is released straight to 'available' — this does NOT
    immediately try to hand it to the next waiting reservation (see
    flagged note below); the next actual RETURN will trigger
    fulfillment normally.
    """
    permission_classes = [IsAdminOrLibrarian]

    def post(self, request, pk):
        reservation = get_object_or_404(
            scope_queryset_to_library(
                Reservation.objects.select_related('book_copy'),
                request.user,
                library_lookup=RESERVATION_LIBRARY_LOOKUP,
            ),
            pk=pk,
        )

        if reservation.status not in (Reservation.STATUS_WAITING, Reservation.STATUS_RESERVED):
            return Response(
                {'detail': f"Cannot cancel a reservation with status '{reservation.status}'."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        with transaction.atomic():
            book_copy = reservation.book_copy  # None if still 'waiting'

            reservation.status = Reservation.STATUS_CANCELLED
            reservation.save(update_fields=['status'])

            # if book_copy is not None:
            #     book_copy.status = book_copy.STATUS_AVAILABLE
            #     book_copy.save(update_fields=['status'])

            if book_copy is not None and book_copy.status == book_copy.STATUS_RESERVED:
                assign_or_release_book_copy(book_copy)

            notify_reservation_cancelled(reservation.id)

        return Response({'detail': 'Reservation cancelled successfully.'}, status=status.HTTP_200_OK)