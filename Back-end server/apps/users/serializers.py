from rest_framework import serializers
from django.contrib.auth.password_validation import validate_password
from .models import User, UserRole, UserStatus
from apps.libraries.models import Library


# ---------------------------------------------------------------------------
# CREATE USER SERIALIZER
# ---------------------------------------------------------------------------
# Used when an admin/librarian registers a new system user (POST /api/v1/users/).
# Handles input validation and secure password hashing before saving.
class UserCreateSerializer(serializers.ModelSerializer):
    # Declared explicitly so clients send "library_id" (a UUID string),
    # but DRF validates it against the real Library table via source="library".
    # Because source="library", the *validated_data* key will be "library"
    # (holding an actual Library model instance) — not "library_id".
    library_id = serializers.PrimaryKeyRelatedField(
        source="library",
        queryset=Library.objects.all(),
    )

    password = serializers.CharField(
        write_only=True,
        min_length=8,
        validators=[validate_password],
    )

    class Meta:
        model = User
        fields = [
            "user_id",
            "library_id",   # <-- was "library"
            "name",
            "email",
            "password",
            "role",
            "status",
        ]
        read_only_fields = ["user_id"]
        extra_kwargs = {
            "password": {"write_only": True},
        }

    # ...validate_email, validate_role unchanged...

    def create(self, validated_data):
        password = validated_data.pop("password")
        library = validated_data.pop("library")  # a Library instance, from library_id input
        user = User.objects.create_user(
            password=password,
            library_id=library.library_id,  # extract the raw UUID the manager expects
            **validated_data,
        )
        return user


# ---------------------------------------------------------------------------
# UPDATE USER SERIALIZER
# ---------------------------------------------------------------------------
# Used for editing an existing user (PATCH /api/v1/users/<id>/).
# Password changes are intentionally excluded — that's a separate endpoint.
class UserUpdateSerializer(serializers.ModelSerializer):

    class Meta:
        model = User
        fields = ["name", "role", "status", "library_id"]

    def validate_role(self, value):
        valid_roles = [r[0] for r in UserRole.CHOICES]
        if value not in valid_roles:
            raise serializers.ValidationError(
                f"Invalid role. Must be one of: {', '.join(valid_roles)}"
            )
        return value

    def validate_status(self, value):
        valid_statuses = [s[0] for s in UserStatus.CHOICES]
        if value not in valid_statuses:
            raise serializers.ValidationError(
                f"Invalid status. Must be one of: {', '.join(valid_statuses)}"
            )
        return value

    def update(self, instance, validated_data):
        """
        `update()` is called by `serializer.save()` when updating an existing object.
        We only update the fields that were actually sent in the request
        (partial update behaviour), leaving everything else unchanged.
        """
        instance.name = validated_data.get("name", instance.name)
        instance.role = validated_data.get("role", instance.role)
        instance.status = validated_data.get("status", instance.status)
        instance.library_id = validated_data.get("library_id", instance.library_id)
        instance.save()
        return instance


# ---------------------------------------------------------------------------
# CHANGE PASSWORD SERIALIZER
# ---------------------------------------------------------------------------
# Used for dedicated password change endpoint (POST /api/v1/users/<id>/change-password/).
class ChangePasswordSerializer(serializers.Serializer):

    # `Serializer` (not ModelSerializer) because we're not saving a model directly —
    # we're validating two fields and then calling set_password() manually.
    current_password = serializers.CharField(write_only=True)
    new_password = serializers.CharField(
        write_only=True,
        min_length=8,
        validators=[validate_password],
    )

    def validate_current_password(self, value):
        """Verify the submitted current password matches what's stored."""
        user = self.context["request"].user
        if not user.check_password(value):
            raise serializers.ValidationError("Current password is incorrect.")
        return value

    def validate(self, attrs):
        """Cross-field validation: new password must differ from current."""
        if attrs["current_password"] == attrs["new_password"]:
            raise serializers.ValidationError(
                {"new_password": "New password must differ from the current password."}
            )
        return attrs


# ---------------------------------------------------------------------------
# USER RESPONSE SERIALIZER
# ---------------------------------------------------------------------------
# Read-only serializer used to format User objects in all API responses.
# Kept separate so we have one consistent shape for all outgoing user data.
class UserResponseSerializer(serializers.ModelSerializer):

    class Meta:
        model = User
        fields = [
            "user_id",
            "library_id",
            "name",
            "email",
            "role",
            "status",
        ]
        # All fields are read-only here — this serializer is only used for output.
        read_only_fields = fields


# ---------------------------------------------------------------------------
# PASSWORD RESET REQUEST SERIALIZER
# ---------------------------------------------------------------------------
# Validates the email submitted on the "forgot password" form.
class PasswordResetRequestSerializer(serializers.Serializer):
    email = serializers.EmailField()

    def validate_email(self, value):
        return value.lower().strip()


# ---------------------------------------------------------------------------
# PASSWORD RESET CONFIRM SERIALIZER
# ---------------------------------------------------------------------------
# Validates the uid, token, and new password submitted on the reset form.
class PasswordResetConfirmSerializer(serializers.Serializer):

    uid = serializers.UUIDField()
    token = serializers.CharField()
    new_password = serializers.CharField(
        write_only=True,
        min_length=8,
        validators=[validate_password],
    )
    confirm_password = serializers.CharField(write_only=True)

    def validate(self, attrs):
        if attrs["new_password"] != attrs["confirm_password"]:
            raise serializers.ValidationError(
                {"confirm_password": "Passwords do not match."}
            )
        return attrs


# ---------------------------------------------------------------------------
# EMAIL ACTIVATION SERIALIZER
# ---------------------------------------------------------------------------
# Validates the uid and token submitted by the frontend after the user
# clicks the activation link in their welcome email.
class EmailActivationSerializer(serializers.Serializer):
    uid = serializers.UUIDField()
    token = serializers.CharField()
