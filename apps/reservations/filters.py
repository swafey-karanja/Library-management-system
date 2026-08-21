import django_filters
from .models import Reservation


class ReservationFilter(django_filters.FilterSet):
    """
    Defines which query params are accepted for filtering the
    reservations list, and how each one translates into a DB lookup.

    Example requests this enables:
        /api/reservations/?status=reserved
        /api/reservations/?status=reserved&status=expired   (multiple)
        /api/reservations/?reserved_after=2025-01-01
        /api/reservations/?reserved_before=2025-06-30
        /api/reservations/?expires_after=2025-07-01
        /api/reservations/?book_copy=<uuid>
        /api/reservations/?member=<uuid>
    """

    # --- Status ---------------------------------------------------------
    # ChoiceFilter restricts input to Reservation.STATUS_CHOICES, so a
    # request like ?status=bogus is rejected with a 400 instead of
    # silently returning zero rows.
    status = django_filters.ChoiceFilter(
        field_name='status',
        choices=Reservation.STATUS_CHOICES,
    )

    # --- Direct FK filters ------------------------------------------------
    # These let the frontend ask "show me all reservations for this
    # specific book copy / member" without needing the search endpoint.
    book_copy = django_filters.UUIDFilter(field_name='book_copy__id')
    member = django_filters.UUIDFilter(field_name='member__id')

    # --- Date range filters -----------------------------------------------
    # `DateTimeFilter` with lookup_expr='gte'/'lte' turns a single query
    # param into a ">= " or "<= " comparison in the SQL WHERE clause.
    # We give them friendlier param names via `field_name` mapping.
    reserved_after = django_filters.DateTimeFilter(
        field_name='reserved_at', lookup_expr='gte'
    )
    reserved_before = django_filters.DateTimeFilter(
        field_name='reserved_at', lookup_expr='lte'
    )
    expires_after = django_filters.DateTimeFilter(
        field_name='expires_at', lookup_expr='gte'
    )
    expires_before = django_filters.DateTimeFilter(
        field_name='expires_at', lookup_expr='lte'
    )

    class Meta:
        model = Reservation
        # Fields listed here without a custom filter above would get
        # simple exact-match filters auto-generated. Since we've
        # explicitly declared every field we want above, this list is
        # just for clarity/documentation.
        fields = [
            'status',
            'book_copy',
            'member',
            'reserved_after',
            'reserved_before',
            'expires_after',
            'expires_before',
        ]