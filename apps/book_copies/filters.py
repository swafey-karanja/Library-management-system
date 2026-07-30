"""
filters.py — BookCopy filtering rules
========================================

This uses `django-filter`, a library that plugs into DRF to turn query
parameters (e.g. ?status=available&condition=new) into database
filters, without us having to manually parse `request.GET` and build
up a queryset by hand.

Install it if you haven't already:
    pip install django-filter

And add it to INSTALLED_APPS in settings.py:
    INSTALLED_APPS = [
        ...
        'django_filters',
    ]
"""

import django_filters

from .models import BookCopy


class BookCopyFilter(django_filters.FilterSet):
    """
    Defines exactly which query parameters are accepted for filtering
    the book_copies list, and how each one translates into a database
    lookup.

    Example requests this enables:
        GET /api/book-copies/?status=available
        GET /api/book-copies/?condition=new
        GET /api/book-copies/?library=<library_uuid>
        GET /api/book-copies/?book_name=potter
        GET /api/book-copies/?status=available&condition=good&book_name=potter
        (filters can be combined — they're always ANDed together)
    """

    # `status` and `condition` are exact-match filters (like `field =
    # value` in SQL) against the model's own CharField choices. Because
    # BookCopy.status / BookCopy.condition already define `choices=`,
    # django-filter automatically restricts input to those valid
    # choices and will return a clear 400 error for anything else.
    status = django_filters.ChoiceFilter(choices=BookCopy.STATUS_CHOICES)
    condition = django_filters.ChoiceFilter(choices=BookCopy.CONDITION_CHOICES)

    # `library` filters by the library's UUID directly, e.g.
    # ?library=550e8400-e29b-41d4-a716-446655440000
    # Since `library` is a real ForeignKey field on the model,
    # django-filter can filter on it automatically — no custom method
    # needed, we just need to declare it so it's exposed as a
    # queryable parameter.
    library = django_filters.UUIDFilter(field_name='library__library_id')

    # `book_name` is NOT a field on BookCopy itself — the book's title
    # lives on the related Book model, reached through the `book`
    # foreign key. `field_name='book__title'` tells django-filter to
    # follow that relationship (Django's double-underscore "__" syntax
    # for traversing foreign keys) and filter on Book.title instead.
    #
    # `lookup_expr='icontains'` makes this a case-insensitive partial
    # match (SQL ILIKE '%value%') rather than requiring an exact title
    # match — e.g. ?book_name=potter will match "Harry Potter and the
    # Philosopher's Stone".
    book_name = django_filters.CharFilter(
        field_name='book__title',
        lookup_expr='icontains',
    )

    class Meta:
        model = BookCopy
        # Fields already fully declared above with explicit filter
        # types — listing them here again isn't required, but Meta.fields
        # is left empty on purpose (see class attributes above) since
        # each field needed custom lookup behaviour (icontains, choices,
        # relationship traversal) rather than django-filter's defaults.
        fields = ['status', 'condition', 'library', 'book_name']