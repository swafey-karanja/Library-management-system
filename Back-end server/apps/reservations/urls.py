from django.urls import path
from .views import ReservationListCreateView, ReservationCancelView

app_name = 'reservations'

urlpatterns = [
    # GET  -> list reservations (with filtering/search/pagination)
    # POST -> create a new reservation
    path('', ReservationListCreateView.as_view(), name='reservation-list-create'),

    # POST -> cancel a specific reservation by its UUID primary key
    path('reservations/<uuid:pk>/cancel/', ReservationCancelView.as_view(), name='reservation-cancel'),
]