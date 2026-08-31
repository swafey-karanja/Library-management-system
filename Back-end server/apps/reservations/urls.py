from django.urls import path
from .views import ReservationListCreateView, ReservationCancelView

app_name = 'reservations'

urlpatterns = [
    path('', ReservationListCreateView.as_view(), name='reservation-list-create'),
    path('<uuid:pk>/cancel/', ReservationCancelView.as_view(), name='reservation-cancel'),
]