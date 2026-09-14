from django.urls import path
from .views import (
    InventoryListView
)

urlpatterns = [
    path('', InventoryListView.as_view(), name='list'),
]