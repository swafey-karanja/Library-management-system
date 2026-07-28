from django.urls import path
from .views import (
    MemberListView,
    MemberCreateView,
    MemberUpdateView,
    MemberStatisticsView,
    MemberExportView,
    MemberImportView,
)

urlpatterns = [
    path("", MemberListView.as_view(), name="list-members"),
    path("create/", MemberCreateView.as_view(), name="create-member"),
    path("<uuid:member_id>/update/", MemberUpdateView.as_view(), name="update-member"),
    path("statistics/", MemberStatisticsView.as_view(), name="member-statistics"),
    path("export/", MemberExportView.as_view(), name="export-members"),
    path("import/", MemberImportView.as_view(), name="import-members"),
]