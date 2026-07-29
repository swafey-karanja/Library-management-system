"""
books/filters.py

WHAT IS django-filter?
    django-filter is a third-party library that plugs into DRF and lets
    you declare "which query params are allowed, and what DB lookup each
    one performs" as a class, instead of writing manual if-statements in
    your view like:

        if 'genre' in request.query_params:
            queryset = queryset.filter(genre=request.query_params['genre'])
        if 'publication_year' in request.query_params:
            ...

    Install it first (it's not part of DRF by default):

        pip install django-filter

    Then add it to INSTALLED_APPS in settings.py:

        INSTALLED_APPS = [
            ...
            "django_filters",
        ]

HOW THIS CONNECTS TO THE VIEW:
    We attach `filterset_class = BookFilter` on a view (see views.py),
    alongside `DjangoFilterBackend` in `filter_backends`. DRF then reads
    incoming query params, matches them against the fields declared
    below, and applies the corresponding `.filter()` calls to the
    queryset automatically - all before pagination/serialization happen.
"""

import re

import django_filters

from .models import Book


def tokenize(value):
    """
    Breaks a search string into individual "words", discarding any
    punctuation or whitespace between them.

    WHY THIS EXISTS:
        A plain `icontains` lookup checks for one EXACT literal
        substring. That's too strict for names like "Rowling, J.K." -
        a client searching "J.K Rowling" (reversed order, no comma) or
        "Rowling,J.K." (no space after the comma) would get ZERO
        results, even though a human would clearly recognize both as
        the same author. Punctuation and word order shouldn't matter
        for a "does this roughly match" search.

    HOW IT WORKS:
        `re.split(r"[^\\w]+", value)` splits the string wherever it
        finds one or more characters that are NOT a "word character"
        (word characters = letters, digits, underscore). Every run of
        commas, periods, spaces, hyphens, etc. becomes a split point.

        Example:
            tokenize("Rowling, J.K.")   -> ["Rowling", "J", "K"]
            tokenize("J.K Rowling")     -> ["J", "K", "Rowling"]
            tokenize("Rowling,J.K.")    -> ["Rowling", "J", "K"]

        Notice all three produce the SAME set of tokens (just possibly
        in a different order) - which is exactly what lets all three
        query styles match the same underlying data.

    The final list comprehension `if token` drops empty strings that
    `re.split` can produce at the start/end of the string.
    """
    return [token for token in re.split(r"[^\w]+", value) if token]


def filter_by_tokens(queryset, field_name, value):
    """
    Reusable helper: narrows `queryset` to rows where EVERY token from
    `value` appears SOMEWHERE in `field_name` (case-insensitive),
    regardless of the order the tokens were typed in or what
    punctuation originally separated them.

    Chaining `.filter()` calls in a loop - one per token - combines
    them with AND, the same way chaining .filter() calls anywhere else
    in Django does. So for tokens ["J", "K", "Rowling"], this builds:

        queryset
            .filter(authors__icontains="J")
            .filter(authors__icontains="K")
            .filter(authors__icontains="Rowling")

    ...which becomes one SQL query requiring ALL THREE conditions to be
    true for a row to be included - order doesn't matter because each
    condition is checked independently.
    """
    for token in tokenize(value):
        lookup = f"{field_name}__icontains"
        queryset = queryset.filter(**{lookup: token})
    return queryset


class BookFilter(django_filters.FilterSet):
    """
    Declarative filter definitions for the Book list endpoint.

    Each attribute below becomes an allowed query parameter. For
    example, `publication_year_min` (defined below) lets a client call:

        GET /api/books/?publication_year_min=2000&publication_year_max=2010

    ...to get every book published between 2000 and 2010 inclusive.
    """

    # --- Publication year range -------------------------------------------
    # django_filters.NumberFilter maps a single query param to a single
    # comparison. `lookup_expr` controls WHICH SQL comparison is used:
    #   'gte' -> "greater than or equal to"  (>=)
    #   'lte' -> "less than or equal to"     (<=)
    # `field_name` says which model field this targets - both filters
    # below target the same `publication_year` column, just with
    # different comparisons, so a client can supply one or both bounds.
    publication_year_min = django_filters.NumberFilter(
        field_name="publication_year", lookup_expr="gte"
    )
    publication_year_max = django_filters.NumberFilter(
        field_name="publication_year", lookup_expr="lte"
    )
    # Also allow filtering for an EXACT year match, e.g. ?publication_year=2015
    publication_year = django_filters.NumberFilter(
        field_name="publication_year", lookup_expr="exact"
    )

    # --- Title / subtitle / authors / ISBNs ------------------------------------
    # These absorb what used to be a separate "advanced search" endpoint.
    # Because django-filter combines every filter declared on this class
    # with AND, a client can supply just ONE of these params for a
    # simple single-field search, or SEVERAL at once for a more precise,
    # narrowed-down search - e.g.:
    #
    #   ?title=potter                          -> single field
    #   ?title=potter&authors=rowling&genre=Fantasy   -> multiple, AND'd together
    #
    # Each of these uses `method=` instead of a plain `lookup_expr`,
    # which tells django-filter "don't build the .filter() call
    # yourself - call this method on the FilterSet instead, and let IT
    # decide how to narrow the queryset." This is what lets us plug in
    # the tokenized, punctuation/order-insensitive matching from
    # filter_by_tokens() above instead of a single strict `icontains`.
    title = django_filters.CharFilter(method="filter_title")
    subtitle = django_filters.CharFilter(method="filter_subtitle")
    authors = django_filters.CharFilter(method="filter_authors")
    isbn_10 = django_filters.CharFilter(method="filter_isbn_10")
    isbn_13 = django_filters.CharFilter(method="filter_isbn_13")

    # Every `method=` callable receives the SAME three arguments:
    #   self      - this FilterSet instance
    #   queryset  - the queryset built up so far by earlier filters
    #   name      - the field name this filter was declared for (unused
    #               here since we already know it, but DRF always passes it)
    #   value     - whatever the client put in the query param
    # ...and must return the (possibly narrowed) queryset.
    def filter_title(self, queryset, name, value):
        return filter_by_tokens(queryset, "title", value)

    def filter_subtitle(self, queryset, name, value):
        return filter_by_tokens(queryset, "subtitle", value)

    def filter_authors(self, queryset, name, value):
        return filter_by_tokens(queryset, "authors", value)

    def filter_isbn_10(self, queryset, name, value):
        return filter_by_tokens(queryset, "isbn_10", value)

    def filter_isbn_13(self, queryset, name, value):
        return filter_by_tokens(queryset, "isbn_13", value)

    # --- Genre ----------------------------------------------------------------
    # Genre is more of a fixed category than free text, so we deliberately
    # keep this as a strict 'iexact' (case-insensitive EXACT match)
    # rather than tokenized matching - this avoids "Fantasy" also
    # matching a hypothetical "Fantasy Romance" genre value, which
    # tokenized matching on the word "Fantasy" alone would allow.
    genre = django_filters.CharFilter(field_name="genre", lookup_expr="iexact")

    # --- created_at range -------------------------------------------------------
    # DateFromToRangeFilter automatically generates TWO query params from
    # one declaration: `created_at_after` and `created_at_before`. Django
    # handles the DateTimeField -> date comparison conversion for us.
    created_at = django_filters.DateFromToRangeFilter(field_name="created_at")

    class Meta:
        model = Book
        # These are the "simple" auto-generated exact-match filters -
        # django-filter builds a basic filter for each field name listed
        # here automatically. Every field we actually want filterable is
        # already customized above, so this stays empty - it's here
        # mainly to show how you'd add more fields quickly in the future
        # without writing a custom Filter for every single one.
        fields: list[str] = []