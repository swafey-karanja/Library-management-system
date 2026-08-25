"""
serializers.py — BookCopy serializers

A DRF serializer converts model instances <-> JSON, and validates
incoming data before it's saved.
"""

from rest_framework import serializers

from .models import BookCopy


# Total rows across the whole request (after quantity expansion), not
# just the number of items in the array.
MAX_TOTAL_COPIES_PER_REQUEST = 500

class BookCopySerializer(serializers.ModelSerializer):
    """
    ModelSerializer auto-generates a field per model field (matching
    type + constraints like unique=True, choices=), so we don't have
    to redeclare them by hand.
    """

    # Read-only convenience fields, reaching through the FKs so clients
    # don't need a second request just to get the library/book name.
    library_name = serializers.CharField(source='library.name', read_only=True)
    book_title = serializers.CharField(source='book.title', read_only=True)

    class Meta:
        model = BookCopy

        # Explicit whitelist rather than '__all__', so the API surface is
        # obvious at a glance and won't silently grow with new columns.
        fields = [
            'id',
            'library',
            'library_name',
            'book',
            'book_title',
            'barcode',
            'status',
            'condition',
            'shelf_location',
            'acquired_at',
            'created_at',
        ]

        # System-generated, never client-writable.
        read_only_fields = ['id', 'created_at']

        # NOTE: `barcode` needs no special config here — since the model
        # field has blank=True, DRF infers required=False/allow_blank=True
        # automatically. Omit it and BookCopy.save() auto-generates one
        # from the book's ISBN (see models.py).


class BookCopyBulkUpdateItemSerializer(serializers.Serializer):
    """
    Validates ONE item in a bulk-update request. Deliberately narrow —
    only status/condition/shelf_location/acquired_at are declared, so
    barcode/library/book can never be changed this way even if sent.

    Body shape:
        [{"id": "<uuid>", "status": "borrowed"}, ...]
    """

    id = serializers.UUIDField(required=True)

    # All optional since different rows may update different fields;
    # validate() below requires at least one.
    status = serializers.ChoiceField(choices=BookCopy.STATUS_CHOICES, required=False)
    condition = serializers.ChoiceField(choices=BookCopy.CONDITION_CHOICES, required=False)
    shelf_location = serializers.CharField(
        max_length=100, required=False, allow_null=True, allow_blank=True
    )
    acquired_at = serializers.DateTimeField(required=False, allow_null=True)

    def validate(self, attrs):
        """Runs after per-field validation — require at least one
        updatable field besides id."""
        updatable_fields = {'status', 'condition', 'shelf_location', 'acquired_at'}
        if not updatable_fields.intersection(attrs.keys()):
            raise serializers.ValidationError(
                'At least one of status, condition, shelf_location or '
                'acquired_at must be provided for each item.'
            )
        return attrs


class BookCopyCreateSpecSerializer(serializers.Serializer):
    """
    Validates ONE spec — either a single copy or "quantity copies of
    this book/library". Plain Serializer (not ModelSerializer) since a
    spec doesn't map 1:1 onto a row when quantity > 1.
    """

    # Reach Library/Book through BookCopy's own FK definitions rather
    # than importing them directly (keeps this decoupled from exactly
    # how those apps are structured).
    library = serializers.PrimaryKeyRelatedField(
        queryset=BookCopy._meta.get_field('library').related_model.objects.all()
    )
    book = serializers.PrimaryKeyRelatedField(
        queryset=BookCopy._meta.get_field('book').related_model.objects.all()
    )

    # Optional explicit barcode — only valid when quantity is 1 (checked below).
    barcode = serializers.CharField(max_length=100, required=False, allow_blank=True)

    # Defaults to 1 -> "just one copy" behaves like a plain create.
    quantity = serializers.IntegerField(
        required=False, default=1, min_value=1, max_value=MAX_TOTAL_COPIES_PER_REQUEST
    )

    status = serializers.ChoiceField(choices=BookCopy.STATUS_CHOICES, required=False)
    condition = serializers.ChoiceField(choices=BookCopy.CONDITION_CHOICES, required=False)
    shelf_location = serializers.CharField(
        max_length=100, required=False, allow_blank=True, allow_null=True
    )
    acquired_at = serializers.DateTimeField(
        required=False,
        allow_null=True,
        help_text="If not provided, will be auto-set to current timestamp"
    )

    def validate(self, attrs):
        # A barcode identifies ONE physical copy — it can't be reused
        # across several generated copies, so the two are mutually
        # exclusive.
        if attrs.get('quantity', 1) > 1 and attrs.get('barcode'):
            raise serializers.ValidationError(
                'barcode cannot be set when quantity is greater than 1 — '
                'each generated copy needs its own auto-generated barcode.'
            )
        return attrs