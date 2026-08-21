"""
apps/notifications/models.py

A tiny, Django-managed table (unlike borrow_transactions/book_copies/etc,
which map onto your existing hand-written SQL schema) used purely to
track which scheduled reminder emails have already been sent, so the
periodic scan tasks in tasks.py never send the same reminder twice.

WHY A SEPARATE TABLE INSTEAD OF A COLUMN ON borrow_transactions:
borrow_transactions is `managed = False` - it maps onto SQL you wrote
by hand, and Django won't generate migrations for it. Adding tracking
columns there would mean hand-writing another ALTER TABLE. Keeping
this bookkeeping in its own Django-managed table avoids touching that
schema at all, and keeps "did we email about this yet" entirely inside
the notifications app, where it belongs.

HOW THE DEDUPE WORKS FOR BOTH REMINDER TYPES:
Both reminder types use the exact same (transaction, type, date) shape,
just with different meaning for `sent_for_date`:
  - due_soon: sent_for_date is always the transaction's OWN due_date
    (a fixed calendar day). Since a loan has exactly one due_date,
    this means a due_soon reminder can only ever be logged once per
    transaction, period - which is exactly the "send this once"
    behavior we want.
  - overdue: sent_for_date is TODAY's date at scan time. Since "today"
    changes every day, this allows exactly one overdue reminder per
    transaction PER DAY, which is exactly the "send this once daily,
    for as long as it stays overdue" behavior we want.
The unique constraint below enforces this at the database level too,
not just in application logic - so even if two scan runs somehow
overlapped, only one of them could successfully log a given entry
(the other's bulk_create(ignore_conflicts=True) just silently no-ops
for that row).
"""
import uuid

from django.db import models


class NotificationLog(models.Model):
    TYPE_DUE_SOON = "due_soon"
    TYPE_OVERDUE = "overdue"

    TYPE_CHOICES = [
        (TYPE_DUE_SOON, "Due Soon"),
        (TYPE_OVERDUE, "Overdue"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # Plain UUIDField rather than a ForeignKey to BorrowTransaction -
    # this table's only job is "was X reminded about Y on date Z", it
    # doesn't need Django to enforce a real relation or let us
    # traverse it ORM-style. Keeps this app decoupled from having to
    # import borrow_transactions models at the model-definition level
    # (tasks.py already does its BorrowTransaction imports lazily,
    # inside functions, for the same reason - avoids app-loading-order
    # issues between apps.notifications and apps.borrow_transactions).
    borrow_transaction_id = models.UUIDField(db_index=True)

    notification_type = models.CharField(max_length=20, choices=TYPE_CHOICES)

    # See the module docstring above - meaning of this field differs
    # by notification_type.
    sent_for_date = models.DateField()

    sent_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "notification_log"
        constraints = [
            models.UniqueConstraint(
                fields=["borrow_transaction_id", "notification_type", "sent_for_date"],
                name="uniq_notification_per_transaction_type_date",
            )
        ]
        indexes = [
            models.Index(
                fields=["borrow_transaction_id", "notification_type", "sent_for_date"],
                name="idx_notification_dedup_lookup",
            ),
        ]
        verbose_name = "Notification Log Entry"
        verbose_name_plural = "Notification Log Entries"

    def __str__(self):
        return f"{self.notification_type} for {self.borrow_transaction_id} on {self.sent_for_date}"