from rest_framework import serializers
from django.contrib.auth.password_validation import validate_password
from .models import User, UserRole, UserStatus


# ---------------------------------------------------------------------------
# CREATE USER SERIALIZER
# ---------------------------------------------------------------------------
# Used when an admin/librarian registers a new system user (POST /api/v1/users/).
# Handles input validation and secure password hashing before saving.
class UserCreateSerializer(serializers.ModelSerializer):

    # `write_only=True` means this field is accepted on input but never
    # included in any response — passwords should never be sent back to clients.
    password = serializers.CharField(
        write_only=True,
        min_length=8,
        validators=[validate_password],  # enforces Django's password strength rules
    )

    class Meta:
        model = User
        fields = [
            "user_id",
            "library_id",
            "name",
            "email",
            "password",
            "role",
            "status",
        ]
        # user_id is auto-generated — clients cannot set it
        read_only_fields = ["user_id"]

    def validate_email(self, value):
        """Reject duplicate emails with a clear error message."""
        if User.objects.filter(email=value.lower()).exists():
            raise serializers.ValidationError("A user with this email already exists.")
        return value.lower()

    def validate_role(self, value):
        """Ensure the role is one of the defined choices."""
        valid_roles = [r[0] for r in UserRole.CHOICES]
        if value not in valid_roles:
            raise serializers.ValidationError(
                f"Invalid role. Must be one of: {', '.join(valid_roles)}"
            )
        return value

    def validate_status(self, value):
        """Ensure status is one of the defined choices."""
        valid_statuses = [s[0] for s in UserStatus.CHOICES]
        if value not in valid_statuses:
            raise serializers.ValidationError(
                f"Invalid status. Must be one of: {', '.join(valid_statuses)}"
            )
        return value

    def create(self, validated_data):
        """
        `create()` is called by `serializer.save()` when creating a new object.
        We extract the password and call our custom manager so the password
        gets properly hashed before being stored.
        """
        password = validated_data.pop("password")

        user = User.objects.create_user(
            password=password,
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
            "created_at",
        ]
        # All fields are read-only here — this serializer is only used for output.
        read_only_fields = fields
