import logging

from rest_framework import status
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated, AllowAny
from rest_framework_simplejwt.tokens import RefreshToken

from .models import User
from .serializers import (
    UserCreateSerializer,
    UserUpdateSerializer,
    UserResponseSerializer,
    ChangePasswordSerializer,
)
from .permissions import (
    IsAdminOrLibrarian,
    IsSameUserOrAdmin,
    IsSameUserOrAdminOrLibrarian,
)

logger = logging.getLogger(__name__)
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
        Create a new user.
        library_id is injected from the authenticated user's own library_id
        so a librarian can only ever create users within their own library.
        """
        data = request.data.copy()
        data.setdefault("library_id", str(request.user.library_id))

        serializer = UserCreateSerializer(data=data)
        if serializer.is_valid():
            user = serializer.save()
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
            user.set_password(serializer.validated_data["new_password"])
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

        print("Email:", email)
        print("Password:", password)

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
