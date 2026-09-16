from rest_framework import serializers
from .models import Inventory


# ---------------------------------------------------------------------------
# INVENTORY LIST SERIALIZER
# ---------------------------------------------------------------------------
# A serializer's job is to convert model instances -> JSON (and, for
# writable serializers, JSON -> validated model data). This one is
# read-only: it's only used to format Inventory rows in API responses,
# so every field is listed in read_only_fields.
class InventoryListSerializer(serializers.ModelSerializer):

    class Meta:
        model = Inventory
        fields = [
            "id",
            "library_id",
            "book_id",
            "total_copies",
            "available_copies",
            "borrowed_copies",
            "reserved_copies",
            "updated_at",
        ]
        read_only_fields = fields