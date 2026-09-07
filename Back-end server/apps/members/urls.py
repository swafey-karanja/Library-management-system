from django.urls import path
from .views import (
    MemberListView,
    MemberCreateView,
    MemberUpdateView,
    MemberStatisticsView,
    MemberExportView,
    MemberImportView,
    MemberBulkUpdateView,
    MemberActivationView,
    ResendMemberActivationView,
)

urlpatterns = [
    path("", MemberListView.as_view(), name="list-members"),
    path("add/", MemberCreateView.as_view(), name="create-member"),
    path("<uuid:member_id>/update/", MemberUpdateView.as_view(), name="update-member"),
    path("bulk-update/", MemberBulkUpdateView.as_view(), name="bulk-update-members"),
    path("stats/", MemberStatisticsView.as_view(), name="member-statistics"),
    path("export/", MemberExportView.as_view(), name="export-members"),
    path("import/", MemberImportView.as_view(), name="import-members"),
    # Email confirmation (unauthenticated — a brand-new member confirming
    # their address, they don't have login credentials at this point).
    path("activate/", MemberActivationView.as_view(), name="member-activate"),
    # Admin/librarian trigger to resend the confirmation email if the
    # original 24-hour link expired.
    path(
        "<uuid:member_id>/resend-activation/",
        ResendMemberActivationView.as_view(),
        name="member-resend-activation",
    ),
]