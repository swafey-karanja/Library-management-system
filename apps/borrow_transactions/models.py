"""
models.py — BorrowTransaction model

Maps to the existing `borrow_transactions` table. One row = one loan
of one physical BookCopy to one Member, from checkout to return.

SCHEMA BUG TO FIX: your SQL has
    FOREIGN KEY (member_id) REFERENCES members(id)
but `members`' primary key is `member_id`, not `id`. That constraint
would fail to run as written — change it to
`REFERENCES members(member_id)` (same issue exists on `reservations`
and `fines`).
"""

import uuid
from decimal import Decimal

from django.db import models
from django.utils import timezone


class BorrowTransaction(models.Model):
    """One loan record: who borrowed what, when it's due, and its state."""

    # gen_random_uuid() equivalent on the Python side.
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # String references avoid circular imports between apps.
    # related_name lets you go backwards: some_copy.borrow_transactions.all()
    book_copy = models.ForeignKey(
        'book_copies.BookCopy',
        on_delete=models.CASCADE,
        db_column='book_copy_id',
        related_name='borrow_transactions',
    )
    member = models.ForeignKey(
        'members.Member',
        on_delete=models.CASCADE,
        db_column='member_id',
        related_name='borrow_transactions',
    )

    # Default loan length used by save() to auto-fill due_date.
    DEFAULT_LOAN_PERIOD_DAYS = 14

    # NOT NULL in SQL, no DB default — save() fills these in if blank.
    borrowed_at = models.DateTimeField()
    due_date = models.DateTimeField()

    # NULL = still out. Non-null = returned.
    returned_at = models.DateTimeField(blank=True, null=True)

    # Plain VARCHAR in SQL (no Postgres ENUM here, unlike book_copies.status) —
    # so `choices` is enforced by Django only, not the database.
    STATUS_ACTIVE = 'active'
    STATUS_RETURNED = 'returned'
    STATUS_OVERDUE = 'overdue'
    STATUS_LOST = 'lost'

    STATUS_CHOICES = [
        (STATUS_ACTIVE, 'Active'),
        (STATUS_RETURNED, 'Returned'),
        (STATUS_OVERDUE, 'Overdue'),
        (STATUS_LOST, 'Lost'),
    ]

    status = models.CharField(max_length=30, choices=STATUS_CHOICES, default=STATUS_ACTIVE)

    # DecimalField, not FloatField — avoids binary rounding errors on money.
    FINE_PER_DAY_LATE = Decimal('0.50')
    fine_amount = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'))

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        managed = False
        db_table = 'borrow_transactions'

        # Documentation only — these indexes already exist in Postgres.
        indexes = [
            models.Index(fields=['book_copy']),   # idx_borrow_book_copy_id
            models.Index(fields=['member']),      # idx_borrow_member_id
            models.Index(fields=['status']),      # idx_borrow_status
            models.Index(fields=['borrowed_at', 'due_date'], name='idx_borrow_dates'),
        ]

        ordering = ['-borrowed_at']
        verbose_name = 'Borrow Transaction'
        verbose_name_plural = 'Borrow Transactions'

    def __str__(self):
        return f'{self.member_id} borrowed {self.book_copy_id} ({self.status})'

    def save(self, *args, **kwargs):
        """Auto-fills borrowed_at (now) and due_date (+14 days) if not set."""
        if not self.borrowed_at:
            self.borrowed_at = timezone.now()
        if not self.due_date:
            self.due_date = self.borrowed_at + timezone.timedelta(days=self.DEFAULT_LOAN_PERIOD_DAYS)
        super().save(*args, **kwargs)

    @property
    def is_overdue(self):
        """True if still out and past due_date. Read-only — doesn't change status."""
        if self.returned_at is not None:
            return False
        return timezone.now() > self.due_date

    @property
    def days_overdue(self):
        """Whole days late, using returned_at if returned else now. Never negative."""
        reference_point = self.returned_at or timezone.now()
        if reference_point <= self.due_date:
            return 0
        return (reference_point - self.due_date).days

    def calculate_fine(self):
        """Returns what the fine SHOULD be — doesn't save anything."""
        return self.days_overdue * self.FINE_PER_DAY_LATE