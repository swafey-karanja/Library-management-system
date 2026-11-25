"""
corrections.py — the rules for a librarian "correcting" a loan.

The correction endpoints (PATCH /<pk>/update/ and PATCH /bulk-update/)
let staff fix a due date, change a loan's status, or adjust a fine.
Changing a loan's STATUS is not just editing a column: the loan and its
physical BookCopy have to stay consistent with each other. For example,
marking a loan "returned" must put the copy back on the shelf — unless
the copy has since been handed to someone else's reservation, in which
case it must be left alone.

All of those rules live in ONE function here, plan_correction(), so the
single-update view and the bulk-update view cannot drift apart.

WHY THIS FILE NEVER TOUCHES THE DATABASE
----------------------------------------
plan_correction() only READS the objects it is given and RETURNS a plan
(which columns to write, and what to do to the copy). The views do the
writing, after they have locked the rows. That keeps the rules easy to
read and easy to test, and it keeps the "decide" step separate from the
"write" step.

THE RULES, IN SHORT
-------------------
1. status and returned_at must agree:
     - a RETURNED loan always has a returned_at date
         (if you don't send one, "now" is used);
     - every other status (active / overdue / lost) has NO returned_at
         (re-opening a returned loan clears it for you).
2. returned_at can't be before borrowed_at or in the future; due_date
   can't be before borrowed_at.
3. When a loan is newly marked RETURNED and no fine_amount is sent, the
   fine is calculated from due_date and returned_at, like a normal
   return. If a fine_amount IS sent it is used as-is (a waiver).
4. Copy changes follow this table. The guiding rule is: only change the
   copy if it is still in the state THIS loan put it in. A copy in any
   other state belongs to something else now (a hold, a shelf, a repair)
   and is left alone.

     loan: active/overdue -> active/overdue    copy: nothing
     loan: active/overdue -> lost              copy: checked_out -> lost
     loan: active/overdue/lost -> returned     copy: checked_out/lost -> released
                                                      (to a waiting member, or the shelf)
     loan: returned -> active/overdue          copy must be available -> checked_out,
                                                      otherwise the correction is refused (409)
     loan: lost -> active/overdue              copy lost -> checked_out
                                                      (any state other than lost/checked_out: refused, 409)
     loan: returned -> lost                    refused (400)
"""

from copy import copy as shallow_copy
from dataclasses import dataclass, field
from datetime import timedelta

from django.utils import timezone
from rest_framework.exceptions import APIException

from .models import BorrowTransaction

# Sentinel for "put this copy back": the view hands it to
# assign_or_release_book_copy(), which gives it to the oldest waiting
# reservation if there is one, otherwise puts it on the shelf.
COPY_RELEASE = 'release'

# How far in the future a returned_at may be, to forgive small clock
# differences between the browser/scanner and the server.
FUTURE_TOLERANCE = timedelta(minutes=5)

_LOAN_IS_OUT = (BorrowTransaction.STATUS_ACTIVE, BorrowTransaction.STATUS_OVERDUE)

_NOT_SENT = object()   # distinguishes "returned_at not in the request" from "returned_at: null"


class CorrectionError(Exception):
    """
    A correction that can't be applied. `status_code` is 400 when the
    request itself is inconsistent, 409 when it conflicts with the
    CURRENT state of the copy. The bulk view sets `loan_id` so the
    response can say which item failed.
    """

    def __init__(self, detail, status_code=400, loan_id=None):
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code
        self.loan_id = loan_id

    def as_api_exception(self):
        """The same error as a DRF exception, which DRF turns into a JSON response."""
        exception = APIException(self.detail)
        exception.status_code = self.status_code
        return exception


@dataclass(frozen=True)
class CorrectionPlan:
    # {column name: new value} — ONLY the columns that must be written.
    # Used as the update_fields list, so no other column is ever touched.
    fields: dict = field(default_factory=dict)
    # None, COPY_RELEASE, or the status the copy should be set to.
    copy_action: str | None = None


def plan_correction(loan, book_copy, changes, now=None):
    """
    Decides what a correction means, without writing anything.

      loan       — the BorrowTransaction, freshly read WHILE LOCKED.
      book_copy  — its BookCopy, locked in the same query.
      changes    — {column: new value} from the request (only what was sent).

    Returns a CorrectionPlan, or raises CorrectionError.
    """
    now = now or timezone.now()
    old_status = loan.status

    # Keep only values that actually differ from what's stored. Sending
    # the current value again is a no-op, not a write.
    fields = {name: value for name, value in changes.items() if getattr(loan, name) != value}
    new_status = fields.get('status', old_status)

    # --- Rule 1: status and returned_at must agree ---------------------
    sent_returned_at = changes.get('returned_at', _NOT_SENT)

    if new_status == BorrowTransaction.STATUS_RETURNED:
        if sent_returned_at is None:
            raise CorrectionError('A returned loan needs a returned_at date.')
        if sent_returned_at is _NOT_SENT and loan.returned_at is None:
            fields['returned_at'] = now            # no date sent: it was returned "now"
    else:
        if sent_returned_at is not _NOT_SENT and sent_returned_at is not None:
            raise CorrectionError('returned_at can only be set on a loan whose status is "returned".')
        if loan.returned_at is not None:
            fields['returned_at'] = None           # re-opening / un-returning clears the date

    # --- Rule 2: dates must make sense ----------------------------------
    borrowed_at = fields.get('borrowed_at', loan.borrowed_at)
    due_date = fields.get('due_date', loan.due_date)
    returned_at = fields.get('returned_at', loan.returned_at)

    if ('due_date' in fields or 'borrowed_at' in fields) and due_date < borrowed_at:
        raise CorrectionError('due_date cannot be before borrowed_at.')
    if 'returned_at' in fields and returned_at is not None:
        if returned_at < borrowed_at:
            raise CorrectionError('returned_at cannot be before borrowed_at.')
        if returned_at > now + FUTURE_TOLERANCE:
            raise CorrectionError('returned_at cannot be in the future.')

    # --- Rule 4: what happens to the copy -------------------------------
    copy_action = _copy_action(old_status, new_status, book_copy)

    # --- Rule 3: fine when a loan is newly marked returned ---------------
    if (
        old_status != BorrowTransaction.STATUS_RETURNED
        and new_status == BorrowTransaction.STATUS_RETURNED
        and 'fine_amount' not in changes
    ):
        # Apply the new values to a throwaway copy of the loan so the
        # model's own calculate_fine() does the arithmetic.
        candidate = shallow_copy(loan)
        for name, value in fields.items():
            setattr(candidate, name, value)
        fine = candidate.calculate_fine()
        if fine != loan.fine_amount:
            fields['fine_amount'] = fine

    return CorrectionPlan(fields=fields, copy_action=copy_action)


def _copy_action(old_status, new_status, book_copy):
    """The copy-changes table from the module docstring."""
    S = BorrowTransaction
    copy_status = book_copy.status

    if old_status == new_status or (old_status in _LOAN_IS_OUT and new_status in _LOAN_IS_OUT):
        return None

    if new_status == S.STATUS_LOST:
        if old_status == S.STATUS_RETURNED:
            raise CorrectionError('A returned loan cannot be marked lost.')
        return book_copy.STATUS_LOST if copy_status == book_copy.STATUS_BORROWED else None

    if new_status == S.STATUS_RETURNED:
        if copy_status in (book_copy.STATUS_BORROWED, book_copy.STATUS_LOST):
            return COPY_RELEASE
        return None            # the copy is already on a shelf / hold / in repair: not ours to move

    # From here the loan is being (re-)opened: new_status is active or overdue.
    if old_status == S.STATUS_RETURNED:
        if copy_status != book_copy.STATUS_AVAILABLE:
            raise CorrectionError(
                f'This loan cannot be re-opened: its copy is "{copy_status}", not available.',
                status_code=409,
            )
        return book_copy.STATUS_BORROWED

    # old_status == lost
    if copy_status == book_copy.STATUS_LOST:
        return book_copy.STATUS_BORROWED
    if copy_status == book_copy.STATUS_BORROWED:
        return None
    raise CorrectionError(
        f'This loan cannot be re-opened: its copy is "{copy_status}".',
        status_code=409,
    )