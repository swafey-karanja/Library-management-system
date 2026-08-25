from rest_framework import generics, filters, status
from rest_framework.views import APIView
from rest_framework.response import Response
from django_filters.rest_framework import DjangoFilterBackend
from django.db import transaction
from django.shortcuts import get_object_or_404

from .models import Reservation
from .serializers import ReservationListSerializer, ReservationCreateSerializer
from .filters import ReservationFilter
from .pagination import ReservationPagination
from apps.book_copies.models import BookCopy  # needed for the status constant below


class ReservationListCreateView(generics.ListCreateAPIView):
    """(unchanged from before)"""

    queryset = Reservation.objects.select_related('book_copy', 'member').all()
    filter_backends = [DjangoFilterBackend, filters.SearchFilter]
    filterset_class = ReservationFilter
    search_fields = ['member__name', 'member__email']
    pagination_class = ReservationPagination

    def get_serializer_class(self):
        if self.request.method == 'POST':
            return ReservationCreateSerializer
        return ReservationListSerializer


class ReservationCancelView(APIView):
    """
    POST /api/reservations/<uuid:pk>/cancel/
    """

    def post(self, request, pk):
        reservation = get_object_or_404(
            Reservation.objects.select_related('book_copy'), pk=pk
        )

        if reservation.status != Reservation.STATUS_RESERVED:
            return Response(
                {
                    'detail': f"Cannot cancel a reservation with status "
                              f"'{reservation.status}'."
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        with transaction.atomic():
            reservation.status = Reservation.STATUS_CANCELLED
            reservation.save(update_fields=['status'])

            # Freeing the copy back up using the proper constant,
            # not a raw 'available' string.
            book_copy = reservation.book_copy
            book_copy.status = BookCopy.STATUS_AVAILABLE
            book_copy.save(update_fields=['status'])

        return Response(
            {'detail': 'Reservation cancelled successfully.'},
            status=status.HTTP_200_OK,
        )