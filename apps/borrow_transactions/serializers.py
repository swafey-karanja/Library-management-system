"""
serializers.py — BorrowTransaction serializers

Converts BorrowTransaction <-> JSON and validates input before saving.
"""

from rest_framework import serializers

from .models import BorrowTransaction


class BorrowTransactionSerializer(serializers.ModelSerializer):
    """
    Full read/write serializer — used for listing and viewing loans.

    fine_amount is read-only here: it's set by return logic (views.py),
    not edited directly by a client.
    """

    # Convenience read-only fields, reaching through FKs so clients
    # don't need extra requests just to show a name/title.
    member_name = serializers.CharField(source='member.name', read_only=True)
    book_title = serializers.CharField(source='book_copy.book.title', read_only=True)
    barcode = serializers.CharField(source='book_copy.barcode', read_only=True)

    # ReadOnlyField pulls these from the model's @property methods —
    # they're computed, not real DB columns.
    is_overdue = serializers.ReadOnlyField()
    days_overdue = serializers.ReadOnlyField()

    class Meta:
        model = BorrowTransaction

        # Explicit whitelist so the API surface is obvious at a glance.
        fields = [
            'id',
            'book_copy',
            'barcode',
            'book_title',
            'member',
            'member_name',
            'borrowed_at',
            'due_date',
            'returned_at',
            'status',
            'fine_amount',
            'is_overdue',
            'days_overdue',
            'created_at',
        ]

        read_only_fields = ['id', 'fine_amount', 'created_at']


class BorrowTransactionUpdateSerializer(serializers.ModelSerializer):
    """
    Librarian correction endpoint — e.g. fixing a wrong due_date, a
    mis-set status, or waiving a fine. Unlike BorrowTransactionSerializer,
    fine_amount IS writable here, since that's the whole point of this
    serializer (manual correction, not the normal checkout/return flow).
    """

    class Meta:
        model = BorrowTransaction
        fields = [
            'id',
            'book_copy',
            'member',
            'borrowed_at',
            'due_date',
            'returned_at',
            'status',
            'fine_amount',
            'created_at',
        ]
        read_only_fields = ['id', 'created_at']


# Fields a bulk-update request may touch. book_copy/member reassignment
# is deliberately excluded — that's a structural change, not a
# correction, so it stays single-item-only via BorrowTransactionUpdateView.
BULK_UPDATABLE_FIELDS = ('status', 'due_date', 'returned_at', 'fine_amount')


class BorrowTransactionBulkUpdateItemSerializer(serializers.Serializer):
    """One item of a bulk-update request body: {"id": ..., ...fields}."""

    id = serializers.UUIDField()
    status = serializers.ChoiceField(choices=BorrowTransaction.STATUS_CHOICES, required=False)
    due_date = serializers.DateTimeField(required=False)
    returned_at = serializers.DateTimeField(required=False, allow_null=True)
    fine_amount = serializers.DecimalField(max_digits=10, decimal_places=2, required=False)


class BorrowCheckoutSerializer(serializers.Serializer):
    """
    Validates a "borrow this copy" request.

    Plain Serializer (not ModelSerializer) — the actual create also
    flips the BookCopy's status, which belongs in the view, not here.
    """

    book_copy = serializers.PrimaryKeyRelatedField(
        queryset=BorrowTransaction._meta.get_field('book_copy').related_model.objects.all()
    )
    member = serializers.PrimaryKeyRelatedField(
        queryset=BorrowTransaction._meta.get_field('member').related_model.objects.all()
    )

    # Optional overrides — if omitted, the model's save() fills these
    # in (now / +14 days).
    borrowed_at = serializers.DateTimeField(required=False)
    due_date = serializers.DateTimeField(required=False)

    def validate_book_copy(self, book_copy):
        """A copy can only be borrowed if it's actually on the shelf."""
        if book_copy.status != book_copy.STATUS_AVAILABLE:
            raise serializers.ValidationError(
                f'This copy is not available to borrow (current status: {book_copy.status}).'
            )
        return book_copy


class BorrowReturnSerializer(serializers.Serializer):
    """
    Validates a "return this copy" request. Which transaction is being
    returned comes from the URL (<pk>), not the body.
    """

    # Defaults to now in the view if omitted.
    returned_at = serializers.DateTimeField(required=False)

    # Optional — lets a librarian record wear/damage found on return.
    # Sourced from BookCopy's own choices so the two stay in sync.
    condition = serializers.ChoiceField(
        choices=BorrowTransaction._meta.get_field('book_copy').related_model.CONDITION_CHOICES,
        required=False,
    )


class BorrowBatchCheckoutSerializer(serializers.Serializer):
    """
    Validates "check out several copies to one member" — e.g. a
    librarian scanning a stack of books for one person at the desk.
    """

    member = serializers.PrimaryKeyRelatedField(
        queryset=BorrowTransaction._meta.get_field('member').related_model.objects.all()
    )
    book_copies = serializers.PrimaryKeyRelatedField(
        queryset=BorrowTransaction._meta.get_field('book_copy').related_model.objects.all(),
        many=True,
    )

    # Shared across the whole batch — if omitted, the model's save()
    # fills these in per-transaction (now / +14 days).
    borrowed_at = serializers.DateTimeField(required=False)
    due_date = serializers.DateTimeField(required=False)

    def validate_book_copies(self, book_copies):
        if not book_copies:
            raise serializers.ValidationError('At least one book copy is required.')
        copy_ids = [copy.pk for copy in book_copies]
        if len(copy_ids) != len(set(copy_ids)):
            raise serializers.ValidationError('The same book copy was listed more than once.')
        return book_copies


class BorrowBatchReturnItemSerializer(serializers.Serializer):
    """One item of a batch-return request: which transaction, and its condition."""

    id = serializers.UUIDField()
    condition = serializers.ChoiceField(
        choices=BorrowTransaction._meta.get_field('book_copy').related_model.CONDITION_CHOICES,
        required=False,
    )


class BorrowBatchReturnSerializer(serializers.Serializer):
    """
    Validates "return several copies at once" — e.g. a librarian
    scanning a stack of books being handed back by one member.
    """

    # Shared across the whole batch — defaults to now in the view if omitted.
    returned_at = serializers.DateTimeField(required=False)
    transactions = BorrowBatchReturnItemSerializer(many=True)

    def validate_transactions(self, transactions):
        if not transactions:
            raise serializers.ValidationError('At least one transaction is required.')
        ids = [item['id'] for item in transactions]
        if len(ids) != len(set(ids)):
            raise serializers.ValidationError('The same transaction was listed more than once.')
        return transactions