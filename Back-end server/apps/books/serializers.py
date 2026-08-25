from rest_framework import serializers
from .models import Book
import uuid
import re


class BookSerializer(serializers.ModelSerializer):
    """
    Serializer for the Book model.

    A ModelSerializer automatically generates serializer fields based on
    the model's fields, which saves us from redeclaring name/url/etc by
    hand. We still need to override behaviour for a couple of fields
    (book_id, created_at) and the create() method, explained below.
    """

    def validate_isbn_13(self, value):
        """
        Field-level validation hook. DRF automatically calls a method
        named `validate_<field_name>` (if it exists) for that specific
        field, after the basic type checks pass but before the object is
        saved. This runs for BOTH create and update.

        Real-world ISBNs are often typed/copied with hyphens or spaces,
        e.g. "978-3-16-148410-0". Our database column is a strict
        VARCHAR(13) holding only the raw digits, so we normalize here -
        this is the fix for the "value too long for character
        varying(13)" error: the raw digit form always fits in 13 chars,
        the hyphenated form does not.
        """
        if not value:
            # Field is optional (blank=True, null=True on the model), so
            # an empty value is fine - just pass it through untouched.
            return value

        # Strip everything that isn't a digit or a trailing 'X' (ISBN-13
        # is numeric-only, but we keep this regex simple/defensive).
        cleaned = re.sub(r"[^0-9]", "", value)

        if len(cleaned) != 13:
            raise serializers.ValidationError(
                f"ISBN-13 must contain exactly 13 digits (got {len(cleaned)}: '{cleaned}')."
            )

        return cleaned

    def validate_isbn_10(self, value):
        """
        Same normalization as validate_isbn_13, but for the 10-digit
        form. ISBN-10's final check digit can legitimately be the
        letter 'X' (e.g. "043942089X"), so we allow that one letter
        through in addition to digits.
        """
        if not value:
            return value

        cleaned = re.sub(r"[^0-9Xx]", "", value).upper()

        if len(cleaned) != 10:
            raise serializers.ValidationError(
                f"ISBN-10 must contain exactly 10 characters (got {len(cleaned)}: '{cleaned}')."
            )

        return cleaned

    class Meta:
        # Tells the serializer which model to base itself on.
        model = Book

        # Explicitly list every field we want exposed through the API.
        # Being explicit (rather than using '__all__') is good practice —
        # it means if we add a sensitive field to the model later, it
        # won't accidentally get exposed through the API until we
        # deliberately add it here.
        fields = [
            "book_id",
            "title",
            "subtitle",
            "authors",
            "publication_year",
            "genre",
            "description",
            "isbn_13",
            "isbn_10",
            "cover_image",
            "created_at",
        ]

        # read_only_fields means: these fields will be INCLUDED when we
        # serialize a Book OUT to JSON (e.g. in the response after
        # creation), but the client is NOT allowed to set them when
        # sending data IN.
        read_only_fields = [
            "book_id",
            "created_at",
        ]

    def create(self, validated_data):
        """
        Overrides the default ModelSerializer.create() behaviour.

        Why we need this: our Book model has `managed = False`, which
        means Django never generated this table itself, so it has no
        knowledge of the table's DEFAULT gen_random_uuid() at the database
        level. Normally, leaving book_id out entirely and just calling
        Book.objects.create(**validated_data) would send an INSERT
        without a value for book_id, and rely on Postgres to fill it
        in via its DEFAULT clause — but Django's ORM doesn't handle that
        gap automatically for fields it doesn't manage.

        To keep things simple and reliable, we generate the UUID on the
        Python/Django side instead, using the same uuid4 algorithm
        Postgres's gen_random_uuid() effectively uses. This guarantees
        every Book created through the API always gets a valid,
        unique book_id without depending on database-side defaults
        being triggered correctly through the ORM.
        """
        validated_data["book_id"] = uuid.uuid4()
        return Book.objects.create(**validated_data)

    def update(self, instance, validated_data):
        """
        Overrides the default ModelSerializer.update() behaviour.

        `instance` is the existing Library object fetched from the
        database by the view (the one we're updating). `validated_data`
        is the cleaned, validated data sent in the PUT or PATCH request
        body — it will never contain library_id or created_at because
        those are declared as read_only_fields above, so DRF strips them
        out before validation even runs.

        The view controls which method is used — our UpdateAPIView
        supports both PUT and PATCH automatically.
        """

        # For each editable field, update the instance's attribute with
        # the new value if it was provided, or keep the existing value
        # if it wasn't (this is what makes PATCH work correctly).

        instance.title = validated_data.get("title", instance.title)
        instance.subtitle = validated_data.get("subtitle", instance.subtitle)
        instance.authors = validated_data.get("authors", instance.authors)
        instance.publication_year = validated_data.get(
            "publication_year", instance.publication_year
        )
        instance.genre = validated_data.get("genre", instance.genre)
        instance.description = validated_data.get(
            "description", instance.description
        )
        instance.isbn_13 = validated_data.get("isbn_13", instance.isbn_13)
        instance.isbn_10 = validated_data.get("isbn_10", instance.isbn_10)
        instance.cover_image = validated_data.get(
            "cover_image", instance.cover_image
        )

        # Persist the changes to the database. Django's ORM generates an
        # UPDATE SQL statement targeting the row with this instance's
        # primary key (library_id).
        instance.save()

        # Return the updated instance so DRF can serialize it back to
        # JSON for the response.
        return instance


class BookBulkUpdateSerializer(serializers.Serializer):
    """
    NOT a ModelSerializer - this doesn't represent a single Book, it
    represents a REQUEST PAYLOAD shape: "update these specific books'
    genre and/or publication_year". Plain `serializers.Serializer` is
    the right tool whenever you're validating input that doesn't map
    1-to-1 onto one model instance.

    Expected request body:
        {
            "book_ids": ["<uuid>", "<uuid>", ...],
            "genre": "Fantasy",            # optional
            "publication_year": 2015       # optional
        }

    At least one of genre / publication_year must be supplied - only
    setting book_ids with nothing to actually update wouldn't make
    sense, so we enforce that in validate() below.
    """

    # ListField wraps another field and validates that the input is a
    # list where EVERY item passes the inner field's validation - here,
    # every item must be a valid UUID. `min_length=1` rejects an empty
    # list, since there'd be nothing to update.
    book_ids = serializers.ListField(
        child=serializers.UUIDField(), min_length=1, allow_empty=False
    )

    # Both fields optional at this stage - we enforce "at least one
    # provided" in validate() below, since that's a cross-field rule
    # that a single field's own validation can't express.
    genre = serializers.CharField(max_length=100, required=False)
    publication_year = serializers.IntegerField(required=False)

    def validate(self, data):
        """
        Object-level validation hook. Unlike `validate_<field_name>`
        (which checks one field in isolation), this runs after all
        individual fields have already passed their own checks, and
        receives the full validated dict - letting us enforce rules
        that depend on MULTIPLE fields at once.
        """
        if "genre" not in data and "publication_year" not in data:
            raise serializers.ValidationError(
                "Provide at least one of 'genre' or 'publication_year' to update."
            )
        return data
