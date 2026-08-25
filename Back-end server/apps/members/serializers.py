from django.db import IntegrityError, transaction
from rest_framework import serializers
from .models import Member


class MemberSerializer(serializers.ModelSerializer):
    """Serializer for the Member model. Used for both list output and create input."""

    class Meta:
        model = Member
        fields = [
            "member_id",
            "library",
            "membership_no",
            "name",
            "email",
            "phone_number",
            "address",
            "status",
            "gender",
            "membership_type",
            "created_at",
        ]
        # member_id/created_at are server-generated. membership_no is
        # auto-generated in create() below, so it's read-only too.
        read_only_fields = [
            "member_id",
            "created_at",
            "membership_no",
        ]

    def create(self, validated_data):
        # Auto-generate a per-library membership_no, retrying on
        # collision (e.g. two requests creating a member at once).
        max_attempts = 5
        library = validated_data["library"]

        for attempt in range(max_attempts):
            validated_data["membership_no"] = self._generate_membership_no(library, offset=attempt)
            try:
                with transaction.atomic():
                    return Member.objects.create(**validated_data)
            except IntegrityError:
                continue

        raise serializers.ValidationError(
            "Could not generate a unique membership number. Please try again."
        )

    @staticmethod
    def _generate_membership_no(library, offset=0):
        # Format: "<LIB_CODE>-000001", numbering resets per library.
        lib_code = str(library.library_id).split("-")[0].upper()
        next_number = Member.objects.filter(library=library).count() + 1 + offset
        return f"{lib_code}-{next_number:06d}"