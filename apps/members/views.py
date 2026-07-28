import csv

from django.db.models import Count
from django.http import HttpResponse
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import generics, filters, status as http_status
from rest_framework.pagination import PageNumberPagination
from rest_framework.parsers import MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import Member, Status
from .serializers import MemberSerializer
from .filters import MemberFilter
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
    """Caps every members list query at 100 rows per page."""

    page_size = 100
    max_page_size = 100


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


class MemberCreateView(generics.CreateAPIView):
    """POST /members/create/ -> create a member (membership_no auto-generated)"""

    queryset = Member.objects.all()
    serializer_class = MemberSerializer
    permission_classes = [IsAdminOrLibrarian]


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
                serializer.save()
                created.append(serializer.data["membership_no"])
            else:
                errors.append({"row": line_number, "errors": serializer.errors})

        response_status = http_status.HTTP_201_CREATED if not errors else http_status.HTTP_207_MULTI_STATUS
        return Response(
            {"created_count": len(created), "created": created, "errors": errors},
            status=response_status,
        )