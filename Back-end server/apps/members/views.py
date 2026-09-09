import csv
import uuid
import hashlib
from datetime import timedelta

from django.conf import settings
from django.db.models import Count
from django.http import HttpResponse
from django.utils import timezone
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import generics, filters, status as http_status
from rest_framework.pagination import PageNumberPagination
from rest_framework.parsers import MultiPartParser
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import Member, MemberActivationToken, Status, Gender, MembershipType
from .serializers import MemberSerializer, MemberActivationSerializer
from .filters import MemberFilter
from .tasks import send_member_welcome_email_task, send_member_active_email_task
from core.permissions import (
    # IsAdminUser,
    IsAdminOrLibrarian,
    # IsSameUserOrAdmin,
    # IsSameUserOrAdminOrLibrarian,
)


def scoped_members_for(user):
    """
    Shared isolation rule, reused by list/statistics/export so they
    can never drift out of sync with each other:
    admins see all members, everyone else sees only their own library's.
    """
    if user.is_admin:
        return Member.objects.all()
    return Member.objects.filter(library_id=user.library_id)


class MemberPagination(PageNumberPagination):
    """Caps every members list query at 30 rows per page."""

    page_size = 30
    max_page_size = 30


class MemberListView(generics.ListAPIView):
    """
    GET /members/ -> list members.
    GET /members/?search=jane -> quick search: matches "jane" against
        name, membership_no, email, OR phone_number (single term).
    GET /members/?name=jane&email=doe -> advanced search: filters on
        specific fields, combined with AND. Any of name, membership_no,
        email, phone_number can be used together this way.
    GET /members/?membership_type=student&gender=female&status=active
        &created_at_after=2026-01-01&created_at_before=2026-06-01
        -> exact-match filters, combinable with the above.
    GET /members/?ordering=name or ?ordering=-created_at -> sort
        ("-" prefix means descending).

    Isolation: a librarian/admin only sees members belonging to their
    own library. Only an admin can see members across all libraries.
    Enforced in get_queryset() rather than trusting client input.
    """

    serializer_class = MemberSerializer
    permission_classes = [IsAdminOrLibrarian]
    pagination_class = MemberPagination

    # Backends run in order, each narrowing/reordering what the previous
    # one returned. All three operate on top of get_queryset(), so
    # isolation is never bypassed by search/filter/ordering params.
    filter_backends = [filters.SearchFilter, DjangoFilterBackend, filters.OrderingFilter]
    search_fields = ["name", "membership_no", "email", "phone_number"]
    filterset_class = MemberFilter

    # Fields a client is allowed to sort by. Whitelisted explicitly so a
    # client can't sort by a field we don't want exposed for ordering.
    ordering_fields = ["name", "membership_no", "created_at"]
    ordering = ["created_at"]  # default order when ?ordering= isn't given

    def get_queryset(self):
        queryset = scoped_members_for(self.request.user)

        # Default to active members only, unless the client explicitly
        # asked for a status. DjangoFilterBackend overrides this if
        # ?status= is present in the request.
        if "status" not in self.request.query_params:
            queryset = queryset.filter(status=Status.ACTIVE)

        return queryset


def _send_member_activation(member):
    """
    Creates a MemberActivationToken for the given member and QUEUES the
    welcome / "please confirm your email" email — it does not send it
    directly. See apps/members/tasks.py for what actually happens after
    `.delay()` is called below.

    Pulled out into its own module-level function (rather than being
    inlined into MemberCreateView) for the same reason apps/users/views.py
    does the equivalent thing with _send_activation(): it needs to be
    callable from more than one place — here, that's the create view AND
    ResendMemberActivationView below, so a librarian can re-trigger the
    email if the original link expired before the member clicked it.
    """
    # uuid4() generates a cryptographically random 128-bit value — this is
    # the raw token that goes out in the email link. We never store this
    # raw value in the database (see the token_hash comment below).
    raw_token = str(uuid.uuid4())

    # SHA-256 hash of the raw token. If our database were ever leaked,
    # an attacker holding only these hashes couldn't reconstruct the raw
    # tokens (hashing is one-way), so they couldn't activate any accounts.
    # This is the exact same principle as never storing plaintext passwords.
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()

    # If this member already had an unused token from a previous call
    # (e.g. this is a resend), invalidate it first so only the newest
    # link actually works — otherwise an old, previously-emailed link
    # would still be valid alongside the new one.
    MemberActivationToken.objects.filter(member=member, is_used=False).update(
        is_used=True
    )

    MemberActivationToken.objects.create(
        member=member,
        token_hash=token_hash,
        # 24 hours gives the member a reasonable window to check their
        # inbox without the link staying open indefinitely.
        expires_at=timezone.now() + timedelta(hours=24),
    )

    # Build the confirmation URL the FRONTEND is responsible for handling.
    # The frontend reads uid + token from the querystring and POSTs them
    # to /api/v1/members/activate/ (MemberActivationView below) — the
    # backend never renders this page itself.
    frontend_url = settings.FRONTEND_URL.rstrip("/")
    confirmation_url = (
        f"{frontend_url}/members/confirm"
        f"?uid={member.member_id}"
        f"&token={raw_token}"
    )

    # `.delay(...)` — NOT calling send_member_welcome_email_task(...)
    # directly — is what actually puts this on the Celery broker instead
    # of running it inline. This function (and the request/response cycle
    # of whoever called it — MemberCreateView, MemberImportView, or
    # ResendMemberActivationView) returns immediately; a separate worker
    # process picks the task up off the queue and sends the real email
    # whenever it gets to it. Only plain strings are passed (member_id,
    # confirmation_url) — see the "why IDs, not model instances" note in
    # tasks.py for why we don't just pass `member` itself.
    send_member_welcome_email_task.delay(str(member.member_id), confirmation_url)


class MemberCreateView(generics.CreateAPIView):
    """
    POST /members/create/ -> create a member (membership_no auto-generated).

    The member is always created with status "inactive" (enforced in
    MemberSerializer.create(), not here) and a welcome/confirmation email
    is fired immediately after the row is saved.
    """

    queryset = Member.objects.all()
    serializer_class = MemberSerializer
    permission_classes = [IsAdminOrLibrarian]

    def perform_create(self, serializer):
        """
        `perform_create()` is a hook CreateAPIView calls internally, right
        where it would normally just do `serializer.save()`. Overriding it
        (instead of overriding the whole `create()`/`post()` method) means
        we get to run extra code immediately after the Member is saved,
        while still letting DRF handle building the 201 response for us.

        `serializer.save()` returns the model instance that was just
        created — we capture it here so we know exactly who to email.
        """
        member = serializer.save()
        _send_member_activation(member)


class MemberUpdateView(generics.UpdateAPIView):
    """
    PUT/PATCH /members/<uuid:member_id>/update/ -> update a member.

    UpdateAPIView supports both PUT (replace all writable fields, so
    every non-read-only field must be sent) and PATCH (partial update,
    only send the fields you want to change). PATCH is normally what
    a frontend "edit member" form would use.

    member_id/created_at/membership_no stay read-only (see the
    serializer's Meta.read_only_fields), so they're protected from
    being changed here without any extra code.
    """

    queryset = Member.objects.all()
    serializer_class = MemberSerializer
    permission_classes = [IsAdminOrLibrarian]
    lookup_field = "member_id"
    lookup_url_kwarg = "member_id"


class MemberBulkUpdateView(APIView):
    """
    PATCH /members/bulk-update/ -> update status, gender, and/or
    membership_type on multiple members in one request. Only these
    three "choice" columns are supported (not name, email, etc — bulk
    editing free-text fields to the same value rarely makes sense).

    Body:
        {
            "member_ids": ["uuid1", "uuid2", ...],
            "status": "inactive",          # optional
            "gender": "male",              # optional
            "membership_type": "student"   # optional
        }
    At least one of status/gender/membership_type is required.

    Isolation: uses scoped_members_for(), so member_ids belonging to
    another library are silently excluded from the update rather than
    erroring — this avoids leaking which IDs exist in other libraries.
    """

    permission_classes = [IsAdminOrLibrarian]

    # Field name -> set of valid values, built from each model's choices.
    ALLOWED_FIELDS = {
        "status": {value for value, _ in Status.CHOICES},
        "gender": {value for value, _ in Gender.CHOICES},
        "membership_type": {value for value, _ in MembershipType.CHOICES},
    }

    def patch(self, request, *args, **kwargs):
        member_ids = request.data.get("member_ids")
        if not member_ids or not isinstance(member_ids, list):
            return Response(
                {"detail": "member_ids must be a non-empty list."},
                status=http_status.HTTP_400_BAD_REQUEST,
            )

        updates = {}
        for field, valid_values in self.ALLOWED_FIELDS.items():
            if field not in request.data:
                continue
            value = request.data[field]
            if value not in valid_values:
                return Response(
                    {"detail": f"Invalid value for {field}: {value!r}. Valid: {sorted(valid_values)}"},
                    status=http_status.HTTP_400_BAD_REQUEST,
                )
            updates[field] = value

        if not updates:
            return Response(
                {"detail": "Provide at least one of: status, gender, membership_type."},
                status=http_status.HTTP_400_BAD_REQUEST,
            )

        queryset = scoped_members_for(request.user).filter(member_id__in=member_ids)
        updated_count = queryset.update(**updates)

        return Response({"updated_count": updated_count, "fields_updated": updates})


class MemberStatisticsView(APIView):
    """
    GET /members/statistics/ -> aggregate counts, scoped the same way
    as MemberListView (admin = all libraries, otherwise own library only).

    Response shape:
        {
            "total_members": 128,
            "by_status": {"active": 120, "inactive": 8},
            "by_gender": {"male": 60, "female": 68},
            "by_membership_type": {"student": 40, "normal": 88}
        }
    """

    permission_classes = [IsAdminOrLibrarian]

    def get(self, request):
        queryset = scoped_members_for(request.user)
        data = {
            "total_members": queryset.count(),
            "by_status": self._counts_by(queryset, "status"),
            "by_gender": self._counts_by(queryset, "gender"),
            "by_membership_type": self._counts_by(queryset, "membership_type"),
        }
        return Response(data)

    @staticmethod
    def _counts_by(queryset, field_name):
        rows = queryset.values(field_name).annotate(count=Count("member_id"))
        return {row[field_name]: row["count"] for row in rows}


class MemberExportView(generics.ListAPIView):
    """
    GET /members/export/ -> download members as a CSV file.

    Reuses the exact same search/filter/ordering setup as MemberListView
    so a client can export a filtered subset (e.g.
    /members/export/?membership_type=student), but bypasses pagination
    since an export should return every matching row, not one page.
    """

    permission_classes = [IsAdminOrLibrarian]
    filter_backends = [filters.SearchFilter, DjangoFilterBackend, filters.OrderingFilter]
    search_fields = ["name", "membership_no", "email", "phone_number"]
    filterset_class = MemberFilter
    ordering_fields = ["name", "membership_no", "created_at"]
    ordering = ["created_at"]

    CSV_COLUMNS = [
        "member_id", "library_id", "membership_no", "name", "email",
        "phone_number", "address", "gender", "membership_type",
        "status", "created_at",
    ]

    def get_queryset(self):
        return scoped_members_for(self.request.user)

    def list(self, request, *args, **kwargs):
        queryset = self.filter_queryset(self.get_queryset())

        response = HttpResponse(content_type="text/csv")
        response["Content-Disposition"] = 'attachment; filename="members_export.csv"'

        writer = csv.writer(response)
        writer.writerow(self.CSV_COLUMNS)
        for member in queryset.iterator():
            writer.writerow([getattr(member, column) for column in self.CSV_COLUMNS])

        return response


class MemberImportView(APIView):
    """
    POST /members/import/ -> bulk-create members from an uploaded CSV.

    Send as multipart/form-data with:
        - file: the CSV (header row required)
        - library_id: required if the requesting user is an admin
          (admins aren't tied to one library, so we can't infer it).
          Non-admins always import into their own library; library_id
          is ignored for them.

    CSV header should include "name" at minimum. Optional columns:
    email, phone_number, address, gender, membership_type. Any
    "membership_no" column in the file is ignored — it's always
    server-generated (see serializers.MemberSerializer.create()).

    Each row is validated independently, so one bad row doesn't fail
    the whole file. Response lists what succeeded and what didn't.
    """

    permission_classes = [IsAdminOrLibrarian]
    parser_classes = [MultiPartParser]

    def post(self, request, *args, **kwargs):
        upload = request.FILES.get("file")
        if not upload:
            return Response({"detail": "No file uploaded. Send it as 'file'."}, status=http_status.HTTP_400_BAD_REQUEST)

        user = request.user
        library_id = request.data.get("library_id") if user.is_admin else str(user.library_id)
        if not library_id:
            return Response({"detail": "library_id is required."}, status=http_status.HTTP_400_BAD_REQUEST)

        reader = csv.DictReader(upload.read().decode("utf-8-sig").splitlines())

        created, errors = [], []
        for line_number, row in enumerate(reader, start=2):  # row 1 is the header
            row["library"] = library_id
            row.pop("library_id", None)
            row.pop("membership_no", None)

            serializer = MemberSerializer(data=row)
            if serializer.is_valid():
                member = serializer.save()
                # Same as a single create via MemberCreateView: every
                # imported member starts "inactive" and gets the same
                # welcome/confirmation email, one per row.
                _send_member_activation(member)
                created.append(serializer.data["membership_no"])
            else:
                errors.append({"row": line_number, "errors": serializer.errors})

        response_status = http_status.HTTP_201_CREATED if not errors else http_status.HTTP_207_MULTI_STATUS
        return Response(
            {"created_count": len(created), "created": created, "errors": errors},
            status=response_status,
        )


# ---------------------------------------------------------------------------
# MEMBER EMAIL ACTIVATION
# POST /api/v1/members/activate/
#
# Called by the FRONTEND (not typed directly by the member) once they
# click the "Confirm my email address" button/link in their welcome email.
# The frontend reads `uid` and `token` out of the link's querystring and
# POSTs them here as JSON.
#
# permission_classes = [AllowAny] because the member isn't logged in at
# this point — they don't even have an account/password, just a link.
# The uid+token pair itself IS the proof of identity for this one action.
# ---------------------------------------------------------------------------
class MemberActivationView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = MemberActivationSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=http_status.HTTP_400_BAD_REQUEST)

        uid = serializer.validated_data["uid"]  # type: ignore
        raw_token = serializer.validated_data["token"]  # type: ignore

        # --- Look up the member ---
        try:
            member = Member.objects.get(pk=uid)
        except Member.DoesNotExist:
            # Deliberately vague — we don't want to confirm/deny whether a
            # given uid exists at all to an unauthenticated caller.
            return Response(
                {"detail": "Invalid confirmation link."},
                status=http_status.HTTP_400_BAD_REQUEST,
            )

        # If they're already active, the link was simply clicked twice
        # (e.g. double-click, or opened in two tabs). Treat that as a
        # harmless success rather than an error.
        if member.status == Status.ACTIVE:
            return Response(
                {"detail": "This membership has already been confirmed."},
                status=http_status.HTTP_200_OK,
            )

        # --- Look up the token by hashing what was submitted ---
        # We never stored the raw token, only its hash, so to find the
        # matching row we hash the submitted value the same way and
        # compare — same principle as checking a password.
        token_hash = hashlib.sha256(raw_token.encode()).hexdigest()

        try:
            activation_token = MemberActivationToken.objects.get(
                member=member,
                token_hash=token_hash,
            )
        except MemberActivationToken.DoesNotExist:
            return Response(
                {"detail": "Invalid confirmation link."},
                status=http_status.HTTP_400_BAD_REQUEST,
            )

        if not activation_token.is_valid():
            return Response(
                {
                    "detail": "This confirmation link has expired. Ask your "
                    "librarian to resend it."
                },
                status=http_status.HTTP_400_BAD_REQUEST,
            )

        # --- Everything checks out: activate the membership ---
        member.status = Status.ACTIVE
        member.save(update_fields=["status"])

        # Mark the token as used so this exact link can never be replayed.
        activation_token.is_used = True
        activation_token.save(update_fields=["is_used"])

        # Fire the "you're now active" confirmation email. This is a
        # separate, purely informational email from the original welcome
        # one — it doesn't ask the member to do anything further.
        # Same as the welcome email: `.delay()` queues this on the
        # broker for a worker to send, rather than making the member's
        # browser wait on Resend's API before this endpoint responds.
        send_member_active_email_task.delay(str(member.member_id))

        return Response(
            {"detail": "Membership confirmed. You are now an active member."},
            status=http_status.HTTP_200_OK,
        )


# ---------------------------------------------------------------------------
# RESEND MEMBER ACTIVATION EMAIL
# POST /api/v1/members/<member_id>/resend-activation/
#
# For when the original 24-hour link expired before the member got around
# to clicking it. Restricted to admin/librarian since it's the library
# staff who would notice/handle this on the member's behalf (e.g. the
# member calls the front desk saying "my link doesn't work anymore").
# ---------------------------------------------------------------------------
class ResendMemberActivationView(APIView):
    permission_classes = [IsAdminOrLibrarian]

    def post(self, request, member_id):
        try:
            member = Member.objects.get(pk=member_id)
        except Member.DoesNotExist:
            return Response(
                {"detail": "Member not found."}, status=http_status.HTTP_404_NOT_FOUND
            )

        if member.status == Status.ACTIVE:
            return Response(
                {"detail": "This membership is already active."},
                status=http_status.HTTP_400_BAD_REQUEST,
            )

        _send_member_activation(member)

        return Response(
            {"detail": "Confirmation email resent successfully."},
            status=http_status.HTTP_200_OK,
        )