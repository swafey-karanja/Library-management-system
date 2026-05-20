import uuid
from django.contrib.auth.models import AbstractBaseUser, BaseUserManager
from django.db import models


# ---------------------------------------------------------------------------
# USER ROLES — valid values for the `role` column
# ---------------------------------------------------------------------------
# Using constants avoids magic strings scattered across the codebase.
# If you need to add a role, add it here and it's available everywhere.
class UserRole:
    ADMIN = "admin"
    LIBRARIAN = "librarian"
    USER = "user"

    CHOICES = [
        (ADMIN, "Admin"),
        (LIBRARIAN, "Librarian"),
        (USER, "User"),
    ]


# ---------------------------------------------------------------------------
# USER STATUS — valid values for the `status` column
# ---------------------------------------------------------------------------
class UserStatus:
    ACTIVE = "active"
    INACTIVE = "inactive"
    SUSPENDED = "suspended"

    CHOICES = [
        (ACTIVE, "Active"),
        (INACTIVE, "Inactive"),
        (SUSPENDED, "Suspended"),
    ]


# ---------------------------------------------------------------------------
# USER MANAGER
# ---------------------------------------------------------------------------
# A Manager is the interface through which Django performs database queries
# for a model. We customise it because our User uses `email` as the login
# field instead of Django's default `username`.
class UserManager(BaseUserManager):

    def create_user(
        self, email, password, name, role, status, library_id, **extra_fields
    ):
        if not email:
            raise ValueError("An email address is required.")
        if not password:
            raise ValueError("A password is required.")

        email = self.normalize_email(email)
        user = self.model(
            email=email,
            name=name,
            role=role,
            status=status,
            library_id=library_id,
            **extra_fields,
        )
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(
        self, email, password, name, library_id, role="admin", **extra_fields
    ):
        """
        Called by `python manage.py createsuperuser`.
        Forces role=admin and status=active regardless of what is passed in.
        """
        return self.create_user(
            email=email,
            password=password,
            name=name,
            role="admin",
            status="active",
            library_id=library_id,
            **extra_fields,
        )


# ---------------------------------------------------------------------------
# USER MODEL
# ---------------------------------------------------------------------------
# AbstractBaseUser gives us password hashing and the authenticate() machinery
# without forcing Django's default fields (username, first_name, etc.) on us.
# We only define the fields that exist in our PostgreSQL `users` table.
class User(AbstractBaseUser):

    # UUID primary key — matches `user_id UUID PRIMARY KEY` in the schema
    user_id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )

    # Foreign key to the library — stored as a plain UUID field because the
    # `library` table is not yet a Django model. We'll convert this to a
    # proper ForeignKey when we build the library module.
    library_id = models.UUIDField(null=False)

    name = models.CharField(max_length=120)
    email = models.EmailField(max_length=255, unique=True)

    # `password_hash` is the column name in PostgreSQL.
    # AbstractBaseUser stores hashed passwords in a field called `password`,
    # so we map it to the correct DB column using db_column.
    password = models.CharField(max_length=255, db_column="password_hash")

    role = models.CharField(
        max_length=50,
        choices=UserRole.CHOICES,
        default=UserRole.USER,
    )

    status = models.CharField(
        max_length=30,
        choices=UserStatus.CHOICES,
        default=UserStatus.ACTIVE,
    )

    created_at = models.DateTimeField(auto_now_add=True)

    # AbstractBaseUser expects last_login to exist. We keep it nullable
    # so it doesn't require a value, but we don't use it for anything.
    last_login = models.DateTimeField(null=True, blank=True)

    # Tells Django which field is used as the username for authentication.
    USERNAME_FIELD = "email"

    # Fields prompted when running `createsuperuser` (besides email + password).
    REQUIRED_FIELDS = ["name", "library_id", "role"]

    # Attach our custom manager so User.objects.create_user() works correctly.
    objects = UserManager()

    class Meta:
        managed = False  # PostgreSQL table already exists — Django won't alter it
        db_table = "users"

    def __str__(self):
        return f"{self.name} <{self.email}>"

    # ------------------------------------------------------------------
    # Permission helpers
    # ------------------------------------------------------------------
    # We're not using Django's built-in permission/group system.
    # These two properties are the minimum required by DRF's IsAuthenticated
    # and other permission classes that inspect the user object.

    @property
    def is_active(self):
        return self.status == UserStatus.ACTIVE

    @property
    def is_admin(self):
        return self.role == UserRole.ADMIN

    # --- Django admin requirements ---
    # The admin panel checks these two properties before allowing login.
    # We map them to our role system rather than adding separate DB columns.

    @property
    def is_staff(self):
        # Any admin or librarian can access the Django admin panel.
        return self.role in [UserRole.ADMIN, UserRole.LIBRARIAN]

    @property
    def is_superuser(self):
        # Only admins get full unrestricted access in the admin panel.
        return self.role == UserRole.ADMIN

    # Django admin calls this when checking object-level permissions.
    # Returning True for superusers gives admins full access to all models.
    def has_perm(self, perm, obj=None):
        return self.is_superuser

    def has_module_perms(self, app_label):
        return self.is_superuser
