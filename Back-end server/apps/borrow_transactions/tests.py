"""
tests.py — borrow_transactions tests

WHAT'S HERE
-----------
Tests for BorrowReturnView (POST /api/borrow-transactions/<pk>/return/).
More views get their tests added here as each one is reviewed.

HOW THESE TESTS TOUCH THE DATABASE
----------------------------------
Every test class uses Django's `TestCase`. TestCase wraps each test in a
transaction and ROLLS IT BACK at the end, so nothing a test creates is
ever left in the database.

That matters in this project: core/settings.py points the TEST database
at the SAME database as the real one (`TEST: {"NAME": ..., "KEEPDB":
True}`), because the core tables are `managed = False` and Django can't
build them in a fresh test database. Rolled-back TestCase tests are safe
there. `TransactionTestCase` is NOT — it commits for real and then
flushes tables afterwards — so don't add one here unless the tests run
against a separate, throwaway database.

(The "two requests at the very same instant" race itself can only be
reproduced with real threads and real commits, i.e. a
TransactionTestCase. It was verified separately on a scratch Postgres.
The tests below guard the two things that make that race impossible: the
row lock in the SQL, and the "already returned" re-check.)

All values that must be unique in the real database (library names/codes,
barcodes, emails, membership numbers) are random, so a test can never
collide with real data.
"""

import re
import uuid
from datetime import timedelta
from decimal import Decimal

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework.test import APIRequestFactory, force_authenticate

from apps.book_copies.models import BookCopy
from apps.books.models import Book
from apps.borrow_transactions import views as borrow_views
from apps.borrow_transactions.models import BorrowTransaction
from apps.borrow_transactions.views import (
    BorrowBatchCheckoutView,
    BorrowCheckoutView,
    BorrowReturnView,
    BorrowTransactionBulkUpdateView,
    BorrowTransactionStatisticsView,
    BorrowTransactionUpdateView,
)
from apps.inventory.models import Inventory
from apps.libraries.models import Library
from apps.members.models import Member
from apps.reservations.models import Reservation
from apps.users.models import User, UserRole


# ---------------------------------------------------------------------------
# Small factory helpers — build just enough data for a test.
# ---------------------------------------------------------------------------

def _uid():
    return uuid.uuid4().hex


def make_library():
    return Library.objects.create(
        library_code=_uid()[:10], name=f"Test Library {_uid()}", enabled_modules={}
    )


def make_librarian(library):
    user = User(
        library=library, name="Test Librarian", email=f"{_uid()}@test.example",
        role=UserRole.LIBRARIAN, password="not-used",
    )
    user.save()
    return user


def make_admin(library):
    """A system admin (not tied to one library's data: sees every library)."""
    user = User(
        library=library, name="Test Admin", email=f"{_uid()}@test.example",
        role=UserRole.ADMIN, password="not-used",
    )
    user.save()
    return user


def make_book():
    return Book.objects.create(title=f"Test Book {_uid()}", authors="Test Author")


def make_member(library, status="active"):
    return Member.objects.create(
        library=library, membership_no=_uid(), name="Test Member",
        email=f"{_uid()}@test.example", phone_number="0700000000", status=status,
    )


def make_copy(library, book, status=BookCopy.STATUS_BORROWED):
    return BookCopy.objects.create(library=library, book=book, barcode=_uid(), status=status)


def make_loan(copy, member, days_ago=3, loan_days=14):
    borrowed_at = timezone.now() - timedelta(days=days_ago)
    return BorrowTransaction.objects.create(
        book_copy=copy, member=member,
        borrowed_at=borrowed_at, due_date=borrowed_at + timedelta(days=loan_days),
    )


def make_waiting_reservation(library, book, member, minutes_ago=10):
    return Reservation.objects.create(
        library=library, book=book, member=member,
        status=Reservation.STATUS_WAITING,
        reserved_at=timezone.now() - timedelta(minutes=minutes_ago),
    )


class BorrowReturnViewTests(TestCase):
    """POST /api/borrow-transactions/<pk>/return/"""

    def setUp(self):
        self.library = make_library()
        self.librarian = make_librarian(self.library)
        self.book = make_book()
        self.member = make_member(self.library)
        self.copy = make_copy(self.library, self.book)        # currently 'checked_out'
        self.loan = make_loan(self.copy, self.member)

    def return_loan(self, body=None, user=None, pk=None):
        """Calls the view directly, as `user` (default: this library's librarian)."""
        request = APIRequestFactory().post("/unused/", body or {}, format="json")
        force_authenticate(request, user=user or self.librarian)
        return BorrowReturnView.as_view()(request, pk=pk or self.loan.pk)

    # -- normal behaviour ---------------------------------------------------

    def test_return_puts_copy_back_on_shelf_and_updates_inventory(self):
        response = self.return_loan()
        self.assertEqual(response.status_code, 200)

        self.loan.refresh_from_db()
        self.copy.refresh_from_db()
        self.assertEqual(self.loan.status, BorrowTransaction.STATUS_RETURNED)
        self.assertIsNotNone(self.loan.returned_at)
        self.assertEqual(self.loan.fine_amount, Decimal("0.00"))
        self.assertEqual(self.copy.status, BookCopy.STATUS_AVAILABLE)

        inventory = Inventory.objects.get(library=self.library, book=self.book)
        self.assertEqual(
            (inventory.total_copies, inventory.available_copies, inventory.borrowed_copies),
            (1, 1, 0),
        )

    def test_late_return_records_fine(self):
        # Borrowed 20 days ago for 14 days -> due 6 days ago -> 6 x 0.50.
        late_loan = make_loan(make_copy(self.library, self.book), self.member, days_ago=20)
        response = self.return_loan(pk=late_loan.pk)
        self.assertEqual(response.status_code, 200)
        late_loan.refresh_from_db()
        self.assertEqual(late_loan.fine_amount, Decimal("3.00"))

    def test_oldest_waiting_reservation_gets_the_copy(self):
        oldest = make_waiting_reservation(self.library, self.book, make_member(self.library), 20)
        newer = make_waiting_reservation(self.library, self.book, make_member(self.library), 10)

        self.assertEqual(self.return_loan().status_code, 200)

        oldest.refresh_from_db()
        newer.refresh_from_db()
        self.copy.refresh_from_db()
        self.assertEqual(oldest.status, Reservation.STATUS_RESERVED)
        self.assertEqual(oldest.book_copy_id, self.copy.pk)
        self.assertIsNotNone(oldest.expires_at)
        self.assertEqual(newer.status, Reservation.STATUS_WAITING)
        self.assertEqual(self.copy.status, BookCopy.STATUS_RESERVED)

    def test_damaged_copy_is_not_handed_to_a_waiting_member(self):
        waiting = make_waiting_reservation(self.library, self.book, make_member(self.library))

        self.assertEqual(self.return_loan({"condition": "damaged"}).status_code, 200)

        self.copy.refresh_from_db()
        waiting.refresh_from_db()
        self.assertEqual(self.copy.status, BookCopy.STATUS_DAMAGED)
        self.assertEqual(self.copy.condition, BookCopy.CONDITION_DAMAGED)
        self.assertEqual(waiting.status, Reservation.STATUS_WAITING)

    def test_condition_is_saved_when_given(self):
        self.assertEqual(self.return_loan({"condition": "good"}).status_code, 200)
        self.copy.refresh_from_db()
        self.assertEqual(self.copy.status, BookCopy.STATUS_AVAILABLE)
        self.assertEqual(self.copy.condition, BookCopy.CONDITION_GOOD)

    # -- the double-return protection (C1) ----------------------------------

    def test_returning_twice_is_rejected_and_does_not_reassign_the_copy(self):
        """
        The second request must fail cleanly AND must not run the
        "hand the copy to the next waiting member" step again — that was
        the bug: one physical copy ended up reserved for two people.
        """
        first = make_waiting_reservation(self.library, self.book, make_member(self.library), 20)
        second = make_waiting_reservation(self.library, self.book, make_member(self.library), 10)

        self.assertEqual(self.return_loan().status_code, 200)
        again = self.return_loan()

        self.assertEqual(again.status_code, 400)
        self.assertEqual(again.data["detail"], "This transaction has already been returned.")
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(first.status, Reservation.STATUS_RESERVED)
        self.assertEqual(second.status, Reservation.STATUS_WAITING)
        self.assertEqual(
            Reservation.objects.filter(book_copy=self.copy, status=Reservation.STATUS_RESERVED).count(), 1
        )

    def test_loan_and_copy_rows_are_locked_while_returning(self):
        """
        Guards the lock itself: if someone removes select_for_update()
        from the view, the double-return race comes back and nothing
        else would notice. Checks the SQL Postgres is actually sent.
        """
        with CaptureQueriesContext(connection) as queries:
            self.assertEqual(self.return_loan().status_code, 200)

        statements = [re.sub(r"\s+", " ", q["sql"]) for q in queries.captured_queries]
        self.assertTrue(
            any(
                s.startswith('SELECT') and 'FROM "borrow_transactions"' in s
                and s.endswith('FOR NO KEY UPDATE OF "borrow_transactions", "book_copies"')
                for s in statements
            ),
            "The loan/copy lookup must end with FOR NO KEY UPDATE OF \"borrow_transactions\", \"book_copies\"",
        )
        # The copy's status is written exactly once (no redundant second UPDATE).
        self.assertEqual(sum(1 for s in statements if s.startswith('UPDATE "book_copies"')), 1)

    # -- scoping and validation --------------------------------------------

    def test_librarian_from_another_library_gets_404_and_nothing_changes(self):
        outsider = make_librarian(make_library())
        response = self.return_loan(user=outsider)
        self.assertEqual(response.status_code, 404)

        self.loan.refresh_from_db()
        self.copy.refresh_from_db()
        self.assertIsNone(self.loan.returned_at)
        self.assertEqual(self.copy.status, BookCopy.STATUS_BORROWED)

    def test_unknown_transaction_id_is_404(self):
        self.assertEqual(self.return_loan(pk=uuid.uuid4()).status_code, 404)

    def test_invalid_condition_is_400_and_nothing_changes(self):
        response = self.return_loan({"condition": "not-a-condition"})
        self.assertEqual(response.status_code, 400)
        self.loan.refresh_from_db()
        self.assertIsNone(self.loan.returned_at)


# ---------------------------------------------------------------------------
# C3 — library isolation and the "only active members may borrow" rule
# ---------------------------------------------------------------------------

def _call(view_class, method, user, body=None, **url_kwargs):
    """Calls a view directly as `user` and returns the DRF response."""
    factory = APIRequestFactory()
    request = getattr(factory, method)("/unused/", body or {}, format="json")
    force_authenticate(request, user=user)
    return view_class.as_view()(request, **url_kwargs)


class CheckoutLibraryAndMemberRuleTests(TestCase):
    """POST /checkout/ and POST /batch-checkout/"""

    def setUp(self):
        self.library = make_library()
        self.other_library = make_library()
        self.librarian = make_librarian(self.library)
        self.admin = make_admin(self.library)
        self.book = make_book()
        self.copy = make_copy(self.library, self.book, status=BookCopy.STATUS_AVAILABLE)
        self.member = make_member(self.library)
        self.foreign_member = make_member(self.other_library)

    def checkout(self, member, user=None, copy=None):
        return _call(
            BorrowCheckoutView, "post", user or self.librarian,
            {"book_copy": str((copy or self.copy).pk), "member": str(member.pk)},
        )

    def batch_checkout(self, member, copies, user=None):
        return _call(
            BorrowBatchCheckoutView, "post", user or self.librarian,
            {"member": str(member.pk), "book_copies": [str(c.pk) for c in copies]},
        )

    def assert_nothing_borrowed(self, *copies):
        self.assertFalse(BorrowTransaction.objects.filter(book_copy__in=copies).exists())
        for copy in copies:
            copy.refresh_from_db()
            self.assertEqual(copy.status, BookCopy.STATUS_AVAILABLE)

    # -- single checkout ----------------------------------------------------

    def test_active_member_of_same_library_can_borrow(self):
        response = self.checkout(self.member)
        self.assertEqual(response.status_code, 201, response.data)
        self.copy.refresh_from_db()
        self.assertEqual(self.copy.status, BookCopy.STATUS_BORROWED)

    def test_librarian_cannot_lend_to_a_member_of_another_library(self):
        response = self.checkout(self.foreign_member)
        self.assertEqual(response.status_code, 404)
        self.assert_nothing_borrowed(self.copy)

    def test_admin_cannot_mix_libraries_either(self):
        # Admins see every library, but a loan must still be one library's
        # copy going to that same library's member.
        response = self.checkout(self.foreign_member, user=self.admin)
        self.assertEqual(response.status_code, 400)
        self.assert_nothing_borrowed(self.copy)

    def test_inactive_member_cannot_borrow(self):
        inactive = make_member(self.library, status="inactive")
        response = self.checkout(inactive)
        self.assertEqual(response.status_code, 409)
        self.assert_nothing_borrowed(self.copy)

    # -- batch checkout -----------------------------------------------------

    def test_batch_for_active_member_of_same_library_works(self):
        second = make_copy(self.library, self.book, status=BookCopy.STATUS_AVAILABLE)
        response = self.batch_checkout(self.member, [self.copy, second])
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(BorrowTransaction.objects.filter(member=self.member).count(), 2)

    def test_batch_for_member_of_another_library_is_404(self):
        response = self.batch_checkout(self.foreign_member, [self.copy])
        self.assertEqual(response.status_code, 404)
        self.assert_nothing_borrowed(self.copy)

    def test_batch_admin_cannot_mix_libraries(self):
        response = self.batch_checkout(self.foreign_member, [self.copy], user=self.admin)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(len(response.data["errors"]), 1)
        self.assert_nothing_borrowed(self.copy)

    def test_batch_for_inactive_member_is_rejected(self):
        inactive = make_member(self.library, status="inactive")
        response = self.batch_checkout(inactive, [self.copy])
        self.assertEqual(response.status_code, 409)
        self.assert_nothing_borrowed(self.copy)


class UpdateAndStatisticsIsolationTests(TestCase):
    def setUp(self):
        self.library = make_library()
        self.other_library = make_library()
        self.librarian = make_librarian(self.library)
        self.admin = make_admin(self.library)
        self.book = make_book()
        self.copy = make_copy(self.library, self.book)
        self.member = make_member(self.library)
        self.loan = make_loan(self.copy, self.member)

    def test_update_cannot_move_a_loan_to_another_librarys_copy_or_member(self):
        foreign_copy = make_copy(self.other_library, make_book())
        foreign_member = make_member(self.other_library)
        new_due = (timezone.now() + timedelta(days=30)).isoformat()

        response = _call(
            BorrowTransactionUpdateView, "patch", self.librarian,
            {"book_copy": str(foreign_copy.pk), "member": str(foreign_member.pk), "due_date": new_due},
            pk=self.loan.pk,
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.loan.refresh_from_db()
        # book_copy / member are ignored (read-only); a normal field still updates.
        self.assertEqual(self.loan.book_copy_id, self.copy.pk)
        self.assertEqual(self.loan.member_id, self.member.pk)
        self.assertGreater(self.loan.due_date, timezone.now() + timedelta(days=29))

    def test_statistics_only_count_the_callers_own_library(self):
        # 1 loan already exists in self.library; add one more here and two elsewhere.
        make_loan(make_copy(self.library, self.book), self.member)
        other_member = make_member(self.other_library)
        other_book = make_book()
        make_loan(make_copy(self.other_library, other_book), other_member)
        make_loan(make_copy(self.other_library, other_book), other_member)

        as_librarian = _call(BorrowTransactionStatisticsView, "get", self.librarian)
        self.assertEqual(as_librarian.status_code, 200)
        self.assertEqual(as_librarian.data["total_transactions"], 2)
        self.assertEqual(as_librarian.data["currently_borrowed"], 2)
        self.assertEqual(as_librarian.data["by_status"]["active"], 2)

        # Admins still see everything (>= because a real database may hold other rows).
        as_admin = _call(BorrowTransactionStatisticsView, "get", self.admin)
        self.assertGreaterEqual(as_admin.data["total_transactions"], 4)


# ---------------------------------------------------------------------------
# C2 — librarian corrections (PATCH /<pk>/update/ and PATCH /bulk-update/)
# ---------------------------------------------------------------------------

def _sql(queries):
    """Captured SQL statements with whitespace collapsed, for easy matching."""
    return [re.sub(r"\s+", " ", q["sql"]) for q in queries.captured_queries]


def _updates_to(statements, table):
    return [s for s in statements if s.startswith(f'UPDATE "{table}"')]


class LoanCorrectionTests(TestCase):
    """PATCH /api/borrow-transactions/<pk>/update/"""

    def setUp(self):
        self.library = make_library()
        self.librarian = make_librarian(self.library)
        self.book = make_book()
        self.member = make_member(self.library)
        self.copy = make_copy(self.library, self.book)          # 'checked_out'
        self.loan = make_loan(self.copy, self.member)           # active, due in ~11 days

    def update(self, body, pk=None, user=None):
        return _call(BorrowTransactionUpdateView, "patch", user or self.librarian, body, pk=pk or self.loan.pk)

    def reload(self):
        self.loan.refresh_from_db()
        self.copy.refresh_from_db()

    def mark_returned_in_db(self):
        """Puts the loan into the 'returned' state, with its copy back on the shelf."""
        BorrowTransaction.objects.filter(pk=self.loan.pk).update(
            status=BorrowTransaction.STATUS_RETURNED, returned_at=timezone.now()
        )
        BookCopy.objects.filter(pk=self.copy.pk).update(status=BookCopy.STATUS_AVAILABLE)

    # -- locking / what gets written ----------------------------------------

    def test_row_is_locked_and_only_the_sent_column_is_written(self):
        new_due = (timezone.now() + timedelta(days=30)).isoformat()
        with CaptureQueriesContext(connection) as queries:
            self.assertEqual(self.update({"due_date": new_due}).status_code, 200)
        statements = _sql(queries)

        self.assertTrue(
            any(s.endswith('FOR NO KEY UPDATE OF "borrow_transactions", "book_copies"') for s in statements),
            "the loan and its copy must be locked before they are changed",
        )
        updates = _updates_to(statements, "borrow_transactions")
        self.assertEqual(len(updates), 1)
        set_clause = re.search(r"SET (.*) WHERE", updates[0]).group(1)
        self.assertIn('"due_date"', set_clause)
        for untouched in ("status", "returned_at", "fine_amount", "member_id", "book_copy_id", "borrowed_at", "created_at"):
            self.assertNotIn(f'"{untouched}"', set_clause, f"{untouched} must not be rewritten")

    def test_request_that_changes_nothing_writes_nothing(self):
        with CaptureQueriesContext(connection) as queries:
            self.assertEqual(self.update({"status": "active"}).status_code, 200)
        statements = _sql(queries)
        self.assertEqual(_updates_to(statements, "borrow_transactions"), [])
        self.assertEqual(_updates_to(statements, "book_copies"), [])

    def test_fine_waiver_changes_only_the_fine(self):
        BorrowTransaction.objects.filter(pk=self.loan.pk).update(fine_amount=Decimal("2.50"))
        self.assertEqual(self.update({"fine_amount": "0.00"}).status_code, 200)
        self.reload()
        self.assertEqual(self.loan.fine_amount, Decimal("0.00"))
        self.assertEqual(self.loan.status, BorrowTransaction.STATUS_ACTIVE)
        self.assertEqual(self.copy.status, BookCopy.STATUS_BORROWED)

    def test_negative_fine_is_rejected(self):
        self.assertEqual(self.update({"fine_amount": "-1.00"}).status_code, 400)

    # -- status changes and the copy ----------------------------------------

    def test_marking_returned_sets_date_fine_and_releases_copy(self):
        late = make_loan(make_copy(self.library, self.book), self.member, days_ago=20)   # 6 days late
        self.assertEqual(self.update({"status": "returned"}, pk=late.pk).status_code, 200)
        late.refresh_from_db()
        self.assertEqual(late.status, "returned")
        self.assertIsNotNone(late.returned_at)
        self.assertEqual(late.fine_amount, Decimal("3.00"))
        late.book_copy.refresh_from_db()
        self.assertEqual(late.book_copy.status, BookCopy.STATUS_AVAILABLE)

    def test_marking_returned_hands_copy_to_oldest_waiter(self):
        oldest = make_waiting_reservation(self.library, self.book, make_member(self.library), 20)
        newer = make_waiting_reservation(self.library, self.book, make_member(self.library), 10)
        self.assertEqual(self.update({"status": "returned"}).status_code, 200)
        oldest.refresh_from_db(); newer.refresh_from_db(); self.copy.refresh_from_db()
        self.assertEqual((oldest.status, newer.status, self.copy.status), ("reserved", "waiting", "reserved"))

    def test_marking_returned_leaves_a_copy_held_for_someone_else_alone(self):
        # The copy was already handed to another member's hold; this loan record
        # being corrected to 'returned' must not hand it out a second time.
        BookCopy.objects.filter(pk=self.copy.pk).update(status=BookCopy.STATUS_RESERVED)
        waiting = make_waiting_reservation(self.library, self.book, make_member(self.library))
        self.assertEqual(self.update({"status": "returned"}).status_code, 200)
        self.reload(); waiting.refresh_from_db()
        self.assertEqual(self.loan.status, "returned")
        self.assertEqual(self.copy.status, BookCopy.STATUS_RESERVED)
        self.assertEqual(waiting.status, Reservation.STATUS_WAITING)

    def test_marking_lost_marks_the_copy_lost(self):
        self.assertEqual(self.update({"status": "lost"}).status_code, 200)
        self.reload()
        self.assertEqual((self.loan.status, self.loan.returned_at, self.copy.status), ("lost", None, "lost"))

    def test_lost_loan_found_and_returned_puts_copy_back(self):
        self.update({"status": "lost"})
        self.assertEqual(self.update({"status": "returned"}).status_code, 200)
        self.reload()
        self.assertEqual((self.loan.status, self.copy.status), ("returned", "available"))

    def test_reopening_a_returned_loan_checks_the_copy_out_again(self):
        self.mark_returned_in_db()
        self.assertEqual(self.update({"status": "active"}).status_code, 200)
        self.reload()
        self.assertEqual((self.loan.status, self.loan.returned_at, self.copy.status), ("active", None, "checked_out"))

    def test_reopening_is_refused_when_the_copy_is_no_longer_available(self):
        self.mark_returned_in_db()
        BookCopy.objects.filter(pk=self.copy.pk).update(status=BookCopy.STATUS_RESERVED)
        self.assertEqual(self.update({"status": "active"}).status_code, 409)
        self.reload()
        self.assertEqual((self.loan.status, self.copy.status), ("returned", "reserved"))
        self.assertIsNotNone(self.loan.returned_at)

    def test_a_returned_loan_cannot_be_marked_lost(self):
        self.mark_returned_in_db()
        self.assertEqual(self.update({"status": "lost"}).status_code, 400)

    # -- status / returned_at must agree, dates must make sense --------------

    def test_returned_status_cannot_have_empty_returned_at(self):
        self.assertEqual(self.update({"status": "returned", "returned_at": None}).status_code, 400)
        self.reload()
        self.assertEqual(self.loan.status, "active")

    def test_returned_at_is_only_allowed_on_a_returned_loan(self):
        self.assertEqual(self.update({"returned_at": timezone.now().isoformat()}).status_code, 400)
        self.assertEqual(
            self.update({"status": "lost", "returned_at": timezone.now().isoformat()}).status_code, 400
        )

    def test_returned_at_before_borrowed_at_is_rejected(self):
        before = (self.loan.borrowed_at - timedelta(days=1)).isoformat()
        self.assertEqual(self.update({"status": "returned", "returned_at": before}).status_code, 400)

    def test_returned_at_in_the_future_is_rejected(self):
        later = (timezone.now() + timedelta(days=2)).isoformat()
        self.assertEqual(self.update({"status": "returned", "returned_at": later}).status_code, 400)

    def test_due_date_before_borrowed_at_is_rejected(self):
        before = (self.loan.borrowed_at - timedelta(days=1)).isoformat()
        self.assertEqual(self.update({"due_date": before}).status_code, 400)

    # -- scoping -------------------------------------------------------------

    def test_other_librarys_loan_is_404_and_unchanged(self):
        outsider = make_librarian(make_library())
        self.assertEqual(self.update({"status": "lost"}, user=outsider).status_code, 404)
        self.reload()
        self.assertEqual((self.loan.status, self.copy.status), ("active", "checked_out"))


class BulkCorrectionTests(TestCase):
    """PATCH /api/borrow-transactions/bulk-update/"""

    def setUp(self):
        self.library = make_library()
        self.librarian = make_librarian(self.library)
        self.book = make_book()
        self.member = make_member(self.library)

    def new_loan(self):
        return make_loan(make_copy(self.library, self.book), self.member)

    def bulk(self, items, user=None):
        return _call(BorrowTransactionBulkUpdateView, "patch", user or self.librarian, items)

    def test_updates_every_item_syncs_copies_and_keeps_request_order(self):
        first, second = self.new_loan(), self.new_loan()
        response = self.bulk([
            {"id": str(second.pk), "status": "returned"},
            {"id": str(first.pk), "status": "lost"},
        ])
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual([row["id"] for row in response.data], [str(second.pk), str(first.pk)])
        for loan in (first, second):
            loan.refresh_from_db(); loan.book_copy.refresh_from_db()
        self.assertEqual((second.status, second.book_copy.status), ("returned", "available"))
        self.assertEqual((first.status, first.book_copy.status), ("lost", "lost"))

    def test_is_all_or_nothing(self):
        good = self.new_loan()
        bad = self.new_loan()
        BorrowTransaction.objects.filter(pk=bad.pk).update(status="returned", returned_at=timezone.now())
        new_due = (timezone.now() + timedelta(days=40)).isoformat()

        response = self.bulk([
            {"id": str(good.pk), "due_date": new_due},
            {"id": str(bad.pk), "status": "lost"},          # a returned loan can't become lost
        ])

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["id"], str(bad.pk))
        good.refresh_from_db()
        self.assertLess(good.due_date, timezone.now() + timedelta(days=20))   # first item rolled back

    def test_oversized_batch_is_rejected(self):
        limit = borrow_views.MAX_BULK_UPDATE_SIZE
        items = [{"id": str(uuid.uuid4()), "status": "lost"} for _ in range(limit + 1)]
        self.assertEqual(self.bulk(items).status_code, 400)

    def test_duplicate_ids_are_rejected(self):
        loan = self.new_loan()
        item = {"id": str(loan.pk), "status": "lost"}
        self.assertEqual(self.bulk([item, item]).status_code, 400)

    def test_unknown_or_other_library_ids_are_reported_missing(self):
        foreign = make_loan(make_copy(make_library(), make_book()), make_member(make_library()))
        response = self.bulk([{"id": str(foreign.pk), "status": "lost"}])
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["missing_ids"], [str(foreign.pk)])

    def test_loans_are_locked_and_only_sent_columns_written(self):
        loans = [self.new_loan(), self.new_loan()]
        new_due = (timezone.now() + timedelta(days=30)).isoformat()
        with CaptureQueriesContext(connection) as queries:
            response = self.bulk([{"id": str(l.pk), "due_date": new_due} for l in loans])
        self.assertEqual(response.status_code, 200, response.data)
        statements = _sql(queries)
        self.assertTrue(any(s.endswith('FOR NO KEY UPDATE OF "borrow_transactions", "book_copies"') for s in statements))
        for update in _updates_to(statements, "borrow_transactions"):
            set_clause = re.search(r"SET (.*) WHERE", update).group(1)
            self.assertEqual(set_clause.count("="), 1, set_clause)
            self.assertIn('"due_date"', set_clause)

    def test_number_of_selects_does_not_grow_with_batch_size(self):
        """The response used to lazy-load member and book once per row (2 extra queries each)."""
        def selects_for(n):
            loans = [self.new_loan() for _ in range(n)]
            new_due = (timezone.now() + timedelta(days=30 + n)).isoformat()
            with CaptureQueriesContext(connection) as queries:
                self.assertEqual(self.bulk([{"id": str(l.pk), "due_date": new_due} for l in loans]).status_code, 200)
            return sum(1 for s in _sql(queries) if s.startswith("SELECT"))

        self.assertEqual(selects_for(2), selects_for(6))


# ---------------------------------------------------------------------------
# C4 — picking up a copy that is being held for a member
# ---------------------------------------------------------------------------

def make_hold(library, book, member, copy, expires_in=timedelta(days=2)):
    """A reservation that has been given a copy: status 'reserved'."""
    return Reservation.objects.create(
        library=library, book=book, member=member, book_copy=copy,
        status=Reservation.STATUS_RESERVED,
        reserved_at=timezone.now() - timedelta(hours=1),
        expires_at=timezone.now() + expires_in,
    )


class ReservedPickupTests(TestCase):
    """
    A copy that has been put on hold ('reserved') for a member must be
    borrowable BY THAT MEMBER (and nobody else), and borrowing it must
    close the hold.
    """

    def setUp(self):
        self.library = make_library()
        self.librarian = make_librarian(self.library)
        self.book = make_book()
        self.holder = make_member(self.library)
        self.other = make_member(self.library)
        self.copy = make_copy(self.library, self.book, status=BookCopy.STATUS_RESERVED)
        self.hold = make_hold(self.library, self.book, self.holder, self.copy)

    def checkout(self, member, copy=None):
        return _call(
            BorrowCheckoutView, "post", self.librarian,
            {"book_copy": str((copy or self.copy).pk), "member": str(member.pk)},
        )

    def batch(self, member, copies):
        return _call(
            BorrowBatchCheckoutView, "post", self.librarian,
            {"member": str(member.pk), "book_copies": [str(c.pk) for c in copies]},
        )

    def assert_hold_untouched(self):
        self.copy.refresh_from_db()
        self.hold.refresh_from_db()
        self.assertEqual(self.copy.status, BookCopy.STATUS_RESERVED)
        self.assertEqual(self.hold.status, Reservation.STATUS_RESERVED)
        self.assertFalse(BorrowTransaction.objects.filter(book_copy=self.copy).exists())

    # -- single -------------------------------------------------------------

    def test_holder_can_pick_up_and_the_hold_is_closed(self):
        response = self.checkout(self.holder)
        self.assertEqual(response.status_code, 201, response.data)
        self.copy.refresh_from_db()
        self.hold.refresh_from_db()
        self.assertEqual(self.copy.status, BookCopy.STATUS_BORROWED)
        self.assertEqual(self.hold.status, Reservation.STATUS_CHECKED_OUT)
        inv = Inventory.objects.get(library=self.library, book=self.book)
        self.assertEqual((inv.borrowed_copies, inv.reserved_copies), (1, 0))

    def test_someone_else_cannot_take_a_held_copy(self):
        response = self.checkout(self.other)
        self.assertEqual(response.status_code, 409)
        self.assert_hold_untouched()

    def test_expired_hold_cannot_be_picked_up(self):
        Reservation.objects.filter(pk=self.hold.pk).update(
            expires_at=timezone.now() - timedelta(minutes=1)
        )
        response = self.checkout(self.holder)
        self.assertEqual(response.status_code, 409)
        self.assert_hold_untouched()

    def test_reserved_copy_with_no_hold_row_is_refused(self):
        Reservation.objects.filter(pk=self.hold.pk).update(status=Reservation.STATUS_CANCELLED)
        response = self.checkout(self.holder)
        self.assertEqual(response.status_code, 409)
        self.copy.refresh_from_db()
        self.assertEqual(self.copy.status, BookCopy.STATUS_RESERVED)

    def test_other_states_are_still_refused(self):
        lost = make_copy(self.library, self.book, status=BookCopy.STATUS_LOST)
        self.assertEqual(self.checkout(self.holder, copy=lost).status_code, 400)  # serializer rejects it early

    def test_lock_is_taken_on_the_reservation_row(self):
        with CaptureQueriesContext(connection) as ctx:
            self.checkout(self.holder)
        locks = [q for q in _sql(ctx)
                 if 'FROM "reservations"' in q and "FOR NO KEY UPDATE" in q]
        self.assertTrue(locks, "reservation row must be locked when claiming a hold")

    # -- batch --------------------------------------------------------------

    def test_batch_mixes_a_pickup_and_a_shelf_copy(self):
        shelf = make_copy(self.library, self.book, status=BookCopy.STATUS_AVAILABLE)
        response = self.batch(self.holder, [self.copy, shelf])
        self.assertEqual(response.status_code, 201, response.data)
        self.copy.refresh_from_db(); shelf.refresh_from_db(); self.hold.refresh_from_db()
        self.assertEqual(self.copy.status, BookCopy.STATUS_BORROWED)
        self.assertEqual(shelf.status, BookCopy.STATUS_BORROWED)
        self.assertEqual(self.hold.status, Reservation.STATUS_CHECKED_OUT)
        inv = Inventory.objects.get(library=self.library, book=self.book)
        self.assertEqual((inv.borrowed_copies, inv.reserved_copies), (2, 0))

    def test_batch_with_someone_elses_hold_is_all_or_nothing(self):
        shelf = make_copy(self.library, self.book, status=BookCopy.STATUS_AVAILABLE)
        response = self.batch(self.other, [shelf, self.copy])
        self.assertEqual(response.status_code, 409)
        self.assertEqual([e["book_copy"] for e in response.data["errors"]], [str(self.copy.pk)])
        shelf.refresh_from_db()
        self.assertEqual(shelf.status, BookCopy.STATUS_AVAILABLE)
        self.assert_hold_untouched()

    def test_batch_with_expired_hold_is_refused(self):
        Reservation.objects.filter(pk=self.hold.pk).update(
            expires_at=timezone.now() - timedelta(minutes=1)
        )
        response = self.batch(self.holder, [self.copy])
        self.assertEqual(response.status_code, 409)
        self.assert_hold_untouched()


# ---------------------------------------------------------------------------
# C5 — a batch return is for ONE member
# ---------------------------------------------------------------------------

class BatchReturnOneMemberTests(TestCase):
    def setUp(self):
        self.library = make_library()
        self.librarian = make_librarian(self.library)
        self.book = make_book()
        self.member_a = make_member(self.library)
        self.member_b = make_member(self.library)
        self.loan_a = make_loan(make_copy(self.library, self.book), self.member_a)
        self.loan_a2 = make_loan(make_copy(self.library, self.book), self.member_a)
        self.loan_b = make_loan(make_copy(self.library, self.book), self.member_b)

    def batch_return(self, *loans):
        from apps.borrow_transactions.views import BorrowBatchReturnView
        return _call(
            BorrowBatchReturnView, "post", self.librarian,
            {"transactions": [{"id": str(loan.pk)} for loan in loans]},
        )

    def test_one_members_stack_is_returned(self):
        response = self.batch_return(self.loan_a, self.loan_a2)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(
            BorrowTransaction.objects.filter(member=self.member_a, returned_at__isnull=False).count(), 2
        )

    def test_mixed_members_are_rejected_and_nothing_changes(self):
        response = self.batch_return(self.loan_a, self.loan_b)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(len(response.data["errors"]), 2)
        for loan in (self.loan_a, self.loan_b):
            loan.refresh_from_db()
            self.assertIsNone(loan.returned_at)
            loan.book_copy.refresh_from_db()
            self.assertEqual(loan.book_copy.status, BookCopy.STATUS_BORROWED)

    def test_return_email_task_sends_each_member_only_their_own_books(self):
        from unittest import mock
        from apps.notifications import tasks
        for loan in (self.loan_a, self.loan_b):
            loan.returned_at = timezone.now()
            loan.save(update_fields=["returned_at"])
        with mock.patch.object(tasks, "_send_email") as send:
            tasks.send_return_email.apply(args=[[str(self.loan_a.pk), str(self.loan_b.pk)]])
        sent = {c.kwargs["to_email"]: c.kwargs["context"]["books"] for c in send.call_args_list}
        self.assertEqual(set(sent), {self.member_a.email, self.member_b.email})
        self.assertEqual([b["barcode"] for b in sent[self.member_a.email]], [self.loan_a.book_copy.barcode])
        self.assertEqual([b["barcode"] for b in sent[self.member_b.email]], [self.loan_b.book_copy.barcode])