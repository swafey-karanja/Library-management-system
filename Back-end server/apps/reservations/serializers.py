from django.utils import timezone
from rest_framework import serializers

from .models import Reservation
from .services import library_carries_book


class ReservationListSerializer(serializers.ModelSerializer):
    """Read-only — used for GET /api/reservations/."""

    member_name = serializers.CharField(source='member.name', read_only=True)
    book_title = serializers.CharField(source='book.title', read_only=True)
    book_copy_barcode = serializers.CharField(source='book_copy.barcode', read_only=True, default=None)
    queue_position = serializers.SerializerMethodField()

    class Meta:
        model = Reservation
        fields = [
            'id', 'book', 'book_title', 'library', 'book_copy', 'book_copy_barcode',
            'member', 'member_name', 'status', 'reserved_at', 'expires_at', 'queue_position',
        ]
        read_only_fields = fields

    def get_queue_position(self, obj):
        return obj.queue_position()


class ReservationCreateSerializer(serializers.ModelSerializer):
    """
    POST /api/reservations/

    Only `book` and `member` are client-supplied. `library` is injected
    by the view from the requesting staff member's OWN library (see
    ReservationListCreateView.perform_create) — never trusted from the
    request body, so a staff user can't place a reservation against
    another library's stock. Every reservation starts 'waiting', with
    no copy assigned — identical for every request, server-controlled.
    """

    class Meta:
        model = Reservation
        fields = ['id', 'book', 'member']
        read_only_fields = ['id']

    def create(self, validated_data):
        # `library` arrives here via the view's serializer.save(library=...)
        # call — DRF merges .save() kwargs into validated_data for create().
        library = validated_data['library']
        book = validated_data['book']

        if not library_carries_book(library, book):
            raise serializers.ValidationError({
                'book': 'This library does not carry any copies of this book.'
            })

        return Reservation.objects.create(
            book=book,
            library=library,
            member=validated_data['member'],
            status=Reservation.STATUS_WAITING,
            reserved_at=timezone.now(),
        )