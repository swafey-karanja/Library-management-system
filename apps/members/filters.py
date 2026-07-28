import django_filters
from .models import Member


class MemberFilter(django_filters.FilterSet):
    """
    Exact-match filters: membership_type, gender, status.

    Advanced search: name, membership_no, email, phone_number each do a
    partial (icontains) match and can be combined in one request (AND),
    e.g. ?name=jane&email=doe -> name contains "jane" AND email
    contains "doe". Omit any of these to not filter on that field.

    created_at supports a date range via ?created_at_after=&created_at_before=
    (both optional, either can be used alone).
    """

    name = django_filters.CharFilter(field_name="name", lookup_expr="icontains")
    email = django_filters.CharFilter(field_name="email", lookup_expr="icontains")
    phone_number = django_filters.CharFilter(field_name="phone_number", lookup_expr="icontains")

    created_at_after = django_filters.DateFilter(field_name="created_at", lookup_expr="gte")
    created_at_before = django_filters.DateFilter(field_name="created_at", lookup_expr="lte")

    class Meta:
        model = Member
        fields = ["membership_type", "gender", "status", "name", "email", "phone_number"]