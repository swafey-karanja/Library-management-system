from django.urls import path
from .views import MembersListView, MemberCreateView, MemberUpdateView

urlpatterns = [
    path("", MembersListView.as_view(), name="list-members"),
    path("add/", MemberCreateView.as_view(), name="add-member"),
    path( "<uuid:member_id>/update/", MemberUpdateView.as_view(), name="update-member"),
]