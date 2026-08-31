import django_filters
from .models import Reservation


class ReservationFilter(django_filters.FilterSet):
    """
    /api/reservations/?status=waiting
    /api/reservations/?book=<uuid>
    /api/reservations/?member=<uuid>
    /api/reservations/?reserved_after=2025-01-01&reserved_before=2025-01-31

    `library` is deliberately NOT a filter here — every result is
    already scoped to the caller's own library by the view, so a
    redundant filter param would be misleading.
    """
    status = django_filters.ChoiceFilter(field_name='status', choices=Reservation.STATUS_CHOICES)

    book = django_filters.UUIDFilter(field_name='book__id')
    book_copy = django_filters.UUIDFilter(field_name='book_copy__id')
    member = django_filters.UUIDFilter(field_name='member__id')

    reserved_after = django_filters.DateTimeFilter(field_name='reserved_at', lookup_expr='gte')
    reserved_before = django_filters.DateTimeFilter(field_name='reserved_at', lookup_expr='lte')
    expires_after = django_filters.DateTimeFilter(field_name='expires_at', lookup_expr='gte')
    expires_before = django_filters.DateTimeFilter(field_name='expires_at', lookup_expr='lte')

    class Meta:
        model = Reservation
        fields = [
            'status', 'book', 'book_copy', 'member',
            'reserved_after', 'reserved_before', 'expires_after', 'expires_before',
        ]