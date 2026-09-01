from django.db import IntegrityError, transaction
from rest_framework import serializers
from .models import Member


from django.db.models import Q


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
        read_only_fields = [
            "member_id",
            "created_at",
            "membership_no",
        ]

    def validate(self, attrs):
        """
        DRF calls `validate()` after all individual field validators have
        passed, but before `create()`/`update()` runs. It receives the
        full dict of validated (not-yet-saved) data as `attrs`, which
        makes it the right place to check rules that span more than one
        field — here, "email/phone must be unique *within this library*".

        Note this is a business rule, not a DB constraint: the `members`
        table has no UNIQUE constraint on email or phone, so without this
        check duplicates would be allowed silently.
        """
        # On create, `library` is present in attrs because it's a
        # required field on the model. On update (if this serializer is
        # ever reused with a PATCH/PUT view), the client might only send
        # the fields that changed, so fall back to the existing member's
        # library in that case.
        library = attrs.get("library") or getattr(self.instance, "library", None)
        email = attrs.get("email")
        phone = attrs.get("phone_number")

        # Nothing to check against if neither field was actually sent.
        if library is None or (not email and not phone):
            return attrs

        # Build an OR filter: match on email OR phone, whichever was provided.
        conflict_filter = Q()
        if email:
            conflict_filter |= Q(email__iexact=email)  # case-insensitive match
        if phone:
            conflict_filter |= Q(phone_number=phone)

        # Scope the search to members of *this* library only — the same
        # person is allowed to be a member at a different library with
        # the same email/phone, just not twice at the same one.
        existing_members = Member.objects.filter(library=library).filter(conflict_filter)

        # If we're updating an existing member (not creating), exclude
        # themself from the check — otherwise every update would fail
        # by "conflicting" with its own record.
        if self.instance is not None:
            existing_members = existing_members.exclude(pk=self.instance.pk)

        conflict = existing_members.first()
        if conflict:
            # Report which field actually caused the conflict, so the
            # client gets a useful, field-specific error message.
            if email and conflict.email and conflict.email.lower() == email.lower():
                raise serializers.ValidationError(
                    {"email": "A member with this email already exists in this library."}
                )
            raise serializers.ValidationError(
                {"phone_number": "A member with this phone number already exists in this library."}
            )

        return attrs

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