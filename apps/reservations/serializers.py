from django.utils import timezone
from datetime import timedelta
from rest_framework import serializers
from .models import Reservation
from apps.book_copies.models import BookCopy  # adjust to your actual app/model path


class ReservationListSerializer(serializers.ModelSerializer):
    """(unchanged — used for GET /api/reservations/)"""

    member_name = serializers.CharField(source='member.name', read_only=True)
    book_copy_barcode = serializers.CharField(source='book_copy.barcode', read_only=True)

    class Meta:
        model = Reservation
        fields = [
            'id', 'book_copy', 'book_copy_barcode', 'member', 'member_name',
            'status', 'reserved_at', 'expires_at',
        ]
        read_only_fields = fields


class ReservationCreateSerializer(serializers.ModelSerializer):
    """
    Serializer for POST /api/reservations/ (creating a new reservation).
    """

    class Meta:
        model = Reservation
        fields = ['id', 'book_copy', 'member', 'expires_at']
        read_only_fields = ['id']

    def validate(self, attrs):
        book_copy = attrs['book_copy']

        # Business rule: you can only reserve a copy that's currently
        # available. We compare against BookCopy.STATUS_AVAILABLE
        # (rather than the raw string 'available') so this stays in
        # sync automatically if that constant's value ever changes.
        if book_copy.status != BookCopy.STATUS_AVAILABLE:
            raise serializers.ValidationError({
                'book_copy': f"This copy is not available for reservation "
                              f"(current status: '{book_copy.get_status_display()}')."
            })

        if not attrs.get('expires_at'):
            attrs['expires_at'] = timezone.now() + timedelta(days=3)

        return attrs

    def create(self, validated_data):
        book_copy = validated_data['book_copy']

        # Flip the copy to 'reserved' so it can't be grabbed by
        # someone else while this reservation is pending.
        book_copy.status = BookCopy.STATUS_RESERVED
        book_copy.save(update_fields=['status'])

        reservation = Reservation.objects.create(
            book_copy=book_copy,
            member=validated_data['member'],
            status=Reservation.STATUS_RESERVED,
            reserved_at=timezone.now(),
            expires_at=validated_data['expires_at'],
        )
        return reservation