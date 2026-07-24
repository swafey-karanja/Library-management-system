from rest_framework import generics

from .models import Member
from core.permissions import (
    #  IsAdminUser,
     IsAdminOrLibrarian,
    #  IsSameUserOrAdmin,
    #  IsSameUserOrAdminOrLibrarian,
)
from .serializers import MemberSerializer

# Create your views here.

class MembersListView(generics.ListAPIView):
    permission_classes = [IsAdminOrLibrarian]
    """
        API endpoint for listing all existing Member records.

        generics.ListAPIView handles GET requests and:
            1. Fetches the queryset defined below (every Member row).
            2. Passes that queryset into the serializer with many=True
               (handled internally by DRF), which converts each Member
               instance into a JSON object.
            3. Wraps the result in a 200 OK response as a JSON array, e.g.:
                   [
                       {"Member_id": "...", "name": "Greenfield Member", ...},
                       {"Member_id": "...", "name": "Riverside Member", ...}
                   ]

        Note that we reuse the exact same MemberSerializer used for creation.
        This works because the serializer's `fields` list already includes
        every field we want returned, and `read_only_fields` only affects
        what the client is allowed to SEND, not what gets returned when
        serializing existing data — so it's perfectly suited for both
        reading and writing.
        """

    # Defines the base queryset this view will return. .all() means every
    # row in the Member table, in whatever default order Postgres
    # returns them (we can add explicit ordering later if needed, e.g.
    # ordering by name or created_at).
    queryset = Member.objects.all()

    serializer_class = MemberSerializer


class MemberCreateView(generics.CreateAPIView):
    """POST /members/create/ -> create a member (membership_no auto-generated)"""
    permission_classes = [IsAdminOrLibrarian]

    queryset = Member.objects.all()
    serializer_class = MemberSerializer


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

    
    