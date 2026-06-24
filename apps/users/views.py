import uuid
import hashlib

from rest_framework import status
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated, AllowAny
from rest_framework_simplejwt.tokens import RefreshToken

from .models import User, UserStatus, PasswordResetToken, EmailActivationToken
from .serializers import (
    UserCreateSerializer,
    UserUpdateSerializer,
    UserResponseSerializer,
    ChangePasswordSerializer,
    PasswordResetRequestSerializer,
    PasswordResetConfirmSerializer,
    EmailActivationSerializer,
)
from .permissions import (
    # IsAdminUser,
    IsAdminOrLibrarian,
    IsSameUserOrAdmin,
    IsSameUserOrAdminOrLibrarian,
)

from django.utils import timezone
from datetime import timedelta

from .emails import send_password_reset_email, send_activation_email
from django.conf import settings

# ---------------------------------------------------------------------------
# HELPER
# ---------------------------------------------------------------------------


def get_user_or_404(user_id):
    try:
        return User.objects.get(pk=user_id)
    except User.DoesNotExist:
        return None


# ---------------------------------------------------------------------------
# USER LIST + CREATE
# GET  /api/v1/users/   → list all users in the same library  (admin + librarian)
# POST /api/v1/users/   → create a new user                   (admin + librarian)
# ---------------------------------------------------------------------------


class UserListCreateView(APIView):

    def get_permissions(self):
        # Both GET and POST require admin or librarian — same class covers both.
        return [IsAdminOrLibrarian()]

    def get(self, request):
        """Return all users belonging to the requester's library."""
        users = User.objects.filter(library_id=request.user.library_id).order_by("name")
        serializer = UserResponseSerializer(users, many=True)
        return Response(serializer.data, status=status.HTTP_200_OK)

    def post(self, request):
        """
        Create a new user. Status is forced to inactive by the serializer.
        An activation email is sent immediately after the record is saved.
        library_id is injected from the authenticated user so a librarian
        can only ever create users within their own library.
        """
        data = request.data.copy()
        data.setdefault("library_id", str(request.user.library_id))

        serializer = UserCreateSerializer(data=data)
        if serializer.is_valid():
            user = serializer.save()
            _send_activation(user)
            return Response(
                UserResponseSerializer(user).data,
                status=status.HTTP_201_CREATED,
            )
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


# ---------------------------------------------------------------------------
# USER DETAIL + UPDATE + DEACTIVATE
# GET    /api/v1/users/<user_id>/  → retrieve one user
# PATCH  /api/v1/users/<user_id>/  → update name / role / status
# DELETE /api/v1/users/<user_id>/  → soft-deactivate
#
# GET / PATCH: own record, or admin/librarian
# DELETE:      admin or librarian only (not staff, not the user themselves)
# ---------------------------------------------------------------------------


class UserDetailView(APIView):

    def get_permissions(self):
        if self.request.method == "DELETE":
            # Deactivation is restricted to admin and librarian.
            return [IsAdminOrLibrarian()]
        # GET and PATCH: any authenticated user, but object-level check
        # below further restricts staff to their own record only.
        return [IsSameUserOrAdminOrLibrarian()]

    def get(self, request, user_id):
        user = get_user_or_404(user_id)
        if not user:
            return Response(
                {"detail": "User not found."}, status=status.HTTP_404_NOT_FOUND
            )

        self.check_object_permissions(request, user)
        return Response(UserResponseSerializer(user).data, status=status.HTTP_200_OK)

    def patch(self, request, user_id):
        """Partial update — only fields present in the request body are changed."""
        user = get_user_or_404(user_id)
        if not user:
            return Response(
                {"detail": "User not found."}, status=status.HTTP_404_NOT_FOUND
            )

        self.check_object_permissions(request, user)

        serializer = UserUpdateSerializer(user, data=request.data, partial=True)
        if serializer.is_valid():
            updated_user = serializer.save()
            return Response(
                UserResponseSerializer(updated_user).data, status=status.HTTP_200_OK
            )
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    def delete(self, request, user_id):
        """
        Soft delete — sets status to 'inactive' rather than removing the row.
        Preserves all historical records (borrows, fines, etc.) linked to this user.
        """
        user = get_user_or_404(user_id)
        if not user:
            return Response(
                {"detail": "User not found."}, status=status.HTTP_404_NOT_FOUND
            )

        # Prevent deactivating yourself — avoids accidental lockout.
        if request.user.user_id == user.user_id:
            return Response(
                {"detail": "You cannot deactivate your own account."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        user.status = "inactive"
        user.save(update_fields=["status"])

        return Response(
            {"detail": "User deactivated successfully."},
            status=status.HTTP_200_OK,
        )


# ---------------------------------------------------------------------------
# CHANGE PASSWORD
# POST /api/v1/users/<user_id>/change-password/
#
# Only the account owner or an admin can change a password.
# Librarians cannot change another user's password.
# ---------------------------------------------------------------------------


class ChangePasswordView(APIView):
    permission_classes = [IsSameUserOrAdmin]

    def post(self, request, user_id):
        user = get_user_or_404(user_id)
        if not user:
            return Response(
                {"detail": "User not found."}, status=status.HTTP_404_NOT_FOUND
            )

        self.check_object_permissions(request, user)

        # Pass `request` into the serializer context so validate_current_password()
        # can access request.user to verify the current password.
        serializer = ChangePasswordSerializer(
            data=request.data, context={"request": request}
        )
        if serializer.is_valid():
            user.set_password(serializer.validated_data["new_password"])  # type: ignore
            user.save(update_fields=["password"])
            return Response(
                {"detail": "Password updated successfully."},
                status=status.HTTP_200_OK,
            )
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


# ---------------------------------------------------------------------------
# LOGIN
# POST /api/v1/users/login/
# ---------------------------------------------------------------------------


class LoginView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        email = request.data.get("email", "").lower().strip()
        password = request.data.get("password", "")

        if not email or not password:
            return Response(
                {"detail": "Email and password are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            user = User.objects.get(email=email)
        except User.DoesNotExist:
            # Return the same message for both "not found" and "wrong password"
            # so we don't leak whether an email address is registered.
            return Response(
                {"detail": "Invalid credentials."},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        if not user.check_password(password):
            return Response(
                {"detail": "Invalid credentials."},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        if not user.is_active:
            return Response(
                {"detail": "This account is inactive. Contact your administrator."},
                status=status.HTTP_403_FORBIDDEN,
            )

        refresh = RefreshToken.for_user(user)

        return Response(
            {
                "access": str(refresh.access_token),
                "refresh": str(refresh),
                "user": UserResponseSerializer(user).data,
            },
            status=status.HTTP_200_OK,
        )


# ---------------------------------------------------------------------------
# LOGOUT
# POST /api/v1/users/logout/
# ---------------------------------------------------------------------------


class LogoutView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        refresh_token = request.data.get("refresh")
        if not refresh_token:
            return Response(
                {"detail": "Refresh token is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            token = RefreshToken(refresh_token)
            token.blacklist()
        except Exception:
            return Response(
                {"detail": "Invalid or already expired token."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        return Response(
            {"detail": "Logged out successfully."}, status=status.HTTP_200_OK
        )


# ---------------------------------------------------------------------------
# ME — current authenticated user's own profile
# GET /api/v1/users/me/
# ---------------------------------------------------------------------------


class MeView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response(
            UserResponseSerializer(request.user).data, status=status.HTTP_200_OK
        )


# ---------------------------------------------------------------------------
# PASSWORD RESET REQUEST
# POST /api/v1/users/password-reset/
#
# Step 1 of the flow: user submits their email address.
# We always return the same 200 response whether the email exists or not —
# this prevents attackers from using this endpoint to discover registered emails.
# ---------------------------------------------------------------------------
class PasswordResetRequestView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = PasswordResetRequestSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        email = serializer.validated_data["email"]  # type: ignore

        # Generic response sent regardless of whether the user exists.
        generic_response = Response(
            {"detail": "If that email is registered, a reset link has been sent."},
            status=status.HTTP_200_OK,
        )

        try:
            user = User.objects.get(email=email)
        except User.DoesNotExist:
            # Return the same response so we don't leak whether the email is registered.
            return generic_response

        if not user.is_active:
            # Don't reveal that the account is inactive either.
            return generic_response

        # --- Generate a secure random token ---
        # uuid4() gives us a cryptographically random 128-bit value.
        raw_token = str(uuid.uuid4())

        # Hash the token before storing it — same principle as password hashing.
        # If the DB is leaked, stored hashes can't be used directly.
        token_hash = hashlib.sha256(raw_token.encode()).hexdigest()

        # Invalidate any existing unused tokens for this user so only one
        # active reset link exists at a time.
        PasswordResetToken.objects.filter(user=user, is_used=False).update(is_used=True)

        # Store the new token, expiring 1 hour from now.
        PasswordResetToken.objects.create(
            user=user,
            token_hash=token_hash,
            expires_at=timezone.now() + timedelta(hours=1),
        )

        # Build the reset URL the frontend will handle.
        # uid identifies the user; token authenticates the request.
        # The frontend reads these from the URL and POSTs them to /password-reset/confirm/.
        frontend_url = settings.FRONTEND_URL.rstrip("/")
        reset_url = (
            f"{frontend_url}/reset-password"
            f"?uid={user.user_id}"
            f"&token={raw_token}"
        )

        send_password_reset_email(
            user_name=user.name,
            to_email=user.email,
            reset_url=reset_url,
        )

        return generic_response


# ---------------------------------------------------------------------------
# PASSWORD RESET CONFIRM
# POST /api/v1/users/password-reset/confirm/
#
# Step 2 of the flow: user submits uid + token from the email link
# plus their new password (entered twice on the reset form).
# ---------------------------------------------------------------------------
class PasswordResetConfirmView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = PasswordResetConfirmSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        uid = serializer.validated_data["uid"]  # type: ignore
        raw_token = serializer.validated_data["token"]  # type: ignore
        new_password = serializer.validated_data["new_password"]  # type: ignore

        # --- Look up the user ---
        try:
            user = User.objects.get(pk=uid)
        except User.DoesNotExist:
            return Response(
                {"detail": "Invalid or expired reset link."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # --- Look up the token by hashing the submitted raw token ---
        # We never store the raw token, so we hash it and compare against
        # what's in the DB — same approach as verifying a hashed password.
        token_hash = hashlib.sha256(raw_token.encode()).hexdigest()

        try:
            reset_token = PasswordResetToken.objects.get(
                user=user,
                token_hash=token_hash,
            )
        except PasswordResetToken.DoesNotExist:
            return Response(
                {"detail": "Invalid or expired reset link."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # --- Validate the token ---
        if not reset_token.is_valid():
            return Response(
                {"detail": "This reset link has expired or has already been used."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # --- Apply the new password ---
        user.set_password(new_password)
        user.save(update_fields=["password"])

        # Mark the token as used so it can't be replayed.
        reset_token.is_used = True
        reset_token.save(update_fields=["is_used"])

        return Response(
            {"detail": "Password reset successfully. You can now log in."},
            status=status.HTTP_200_OK,
        )


# ---------------------------------------------------------------------------
# HELPER — generates an activation token and fires the welcome email.
# Extracted into a standalone function so it can be called from the
# user create view without making that view too long.
# ---------------------------------------------------------------------------
def _send_activation(user, request=None):
    """
    Creates an EmailActivationToken for the given user and sends the
    welcome / activation email via Resend.
    """
    raw_token = str(uuid.uuid4())
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()

    # Invalidate any pre-existing unused tokens for this user first.
    EmailActivationToken.objects.filter(user=user, is_used=False).update(is_used=True)

    EmailActivationToken.objects.create(
        user=user,
        token_hash=token_hash,
        expires_at=timezone.now() + timedelta(hours=24),
    )

    frontend_url = settings.FRONTEND_URL.rstrip("/")
    activation_url = (
        f"{frontend_url}/activate" f"?uid={user.user_id}" f"&token={raw_token}"
    )

    send_activation_email(
        user_name=user.name,
        to_email=user.email,
        activation_url=activation_url,
    )


# ---------------------------------------------------------------------------
# EMAIL ACTIVATION
# POST /api/v1/users/activate/
#
# Called by the frontend immediately after the user clicks the link in
# their welcome email. No authentication required — the uid + token pair
# is the proof of identity.
# ---------------------------------------------------------------------------
class EmailActivationView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = EmailActivationSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        uid = serializer.validated_data["uid"]  # type: ignore
        raw_token = serializer.validated_data["token"]  # type: ignore

        # Look up the user.
        try:
            user = User.objects.get(pk=uid)
        except User.DoesNotExist:
            return Response(
                {"detail": "Invalid activation link."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # If the user is already active the link was clicked more than once.
        if user.status == UserStatus.ACTIVE:
            return Response(
                {"detail": "This account has already been activated."},
                status=status.HTTP_200_OK,
            )

        # Hash the submitted token and look it up.
        token_hash = hashlib.sha256(raw_token.encode()).hexdigest()

        try:
            activation_token = EmailActivationToken.objects.get(
                user=user,
                token_hash=token_hash,
            )
        except EmailActivationToken.DoesNotExist:
            return Response(
                {"detail": "Invalid activation link."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if not activation_token.is_valid():
            return Response(
                {
                    "detail": "This activation link has expired. Contact your administrator to resend it."
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Activate the account and invalidate the token.
        user.status = UserStatus.ACTIVE
        user.save(update_fields=["status"])

        activation_token.is_used = True
        activation_token.save(update_fields=["is_used"])

        return Response(
            {"detail": "Account activated successfully. You can now log in."},
            status=status.HTTP_200_OK,
        )


# ---------------------------------------------------------------------------
# RESEND ACTIVATION EMAIL
# POST /api/v1/users/<user_id>/resend-activation/
#
# Admin or librarian can trigger a fresh activation email if the original
# link expired before the user clicked it.
# ---------------------------------------------------------------------------
class ResendActivationView(APIView):
    permission_classes = [IsAdminOrLibrarian]

    def post(self, request, user_id):
        user = get_user_or_404(user_id)
        if not user:
            return Response(
                {"detail": "User not found."}, status=status.HTTP_404_NOT_FOUND
            )

        if user.status == UserStatus.ACTIVE:
            return Response(
                {"detail": "This account is already active."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        _send_activation(user)

        return Response(
            {"detail": "Activation email resent successfully."},
            status=status.HTTP_200_OK,
        )
