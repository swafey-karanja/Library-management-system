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
from apps.borrow_transactions.models import BorrowTransaction
from apps.borrow_transactions.views import (
    BorrowBatchCheckoutView,
    BorrowCheckoutView,
    BorrowReturnView,
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