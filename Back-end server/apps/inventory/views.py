from apps.inventory.models import Inventory
from core.permissions import IsAdminOrLibrarian
from rest_framework import generics, status

# Create your views here.

class InventoryListView(generics.ListAPIView):
    queryset = Inventory.objects.all()
    permission_classes = [IsAdminOrLibrarian]


