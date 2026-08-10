from django.contrib import admin

from .models import BorrowTransaction


@admin.register(BorrowTransaction)
class BorrowTransactionAdmin(admin.ModelAdmin):
    """Registers BorrowTransaction so it's visible/editable in /admin/."""

    list_display = ('id', 'member', 'book_copy', 'status', 'borrowed_at', 'due_date', 'returned_at', 'fine_amount')
    list_filter = ('status',)
    search_fields = ('member__name', 'book_copy__barcode')
    ordering = ('-borrowed_at',)

    # created_at is auto-set (auto_now_add) — never editable, even in admin.
    readonly_fields = ('id', 'created_at')