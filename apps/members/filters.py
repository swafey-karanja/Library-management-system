"""
members/filters.py

See books/filters.py for a fuller explanation of how django-filter and
`method=` filters work - this file follows the same pattern.
"""

import re

import django_filters
from django.db.models import F, Value
from django.db.models.functions import Replace

from .models import Member


def tokenize(value):
    """
    Breaks a search string into individual "words", discarding any
    punctuation or whitespace between them.

    WHY THIS EXISTS:
        A plain `icontains` lookup checks for one EXACT literal
        substring, including punctuation and word order. That's too
        strict for real-world input variance - e.g. a client searching
        "Doe Jane" or "Doe, Jane" or "Doe,Jane" should all find a member
        named "Jane Doe", but a single icontains would only match
        whichever exact spacing/order/punctuation happens to be stored.

    HOW IT WORKS:
        `re.split(r"[^\\w]+", value)` splits the string wherever it
        finds one or more characters that are NOT a "word character"
        (letters, digits, underscore) - so any run of spaces, commas,
        periods, hyphens, etc. becomes a split point.

            tokenize("Doe, Jane")  -> ["Doe", "Jane"]
            tokenize("Jane Doe")   -> ["Jane", "Doe"]

        Both produce the same SET of tokens (just in different order),
        which is exactly what lets both search styles match the same
        underlying data.
    """
    return [token for token in re.split(r"[^\w]+", value) if token]


def filter_by_tokens(queryset, field_name, value):
    """
    Reusable helper: narrows `queryset` to rows where EVERY token from
    `value` appears SOMEWHERE in `field_name` (case-insensitive),
    regardless of the order the tokens were typed in or what
    punctuation originally separated them.

    Chaining `.filter()` calls in a loop - one per token - combines
    them with AND: a row only stays in the queryset if ALL tokens are
    found, each token checked independently (so order never matters).
    """
    for token in tokenize(value):
        lookup = f"{field_name}__icontains"
        queryset = queryset.filter(**{lookup: token})
    return queryset


def normalized_digits_expression(field_name):
    """
    Builds a database expression that strips common phone-number
    formatting characters - spaces, hyphens, parentheses, plus signs,
    and dots - from `field_name`, leaving only the digits.

    WHY THIS IS DIFFERENT FROM tokenize()/filter_by_tokens():
        Names can be reordered ("Jane Doe" vs "Doe, Jane") and still
        make sense split into separate AND'd tokens. Phone numbers
        CAN'T be reordered like that - "555" AND "123" AND "4567" being
        present somewhere doesn't confirm they're the right sequence.
        What actually varies between "+1 (555) 123-4567" and
        "5551234567" is only the FORMATTING punctuation, not the digit
        order - so the right fix is to strip formatting characters from
        BOTH sides of the comparison and compare digits-only, not to
        break the number into independently-matched pieces.

    HOW django.db.models.functions.Replace WORKS:
        Replace(expression, old, new) is Django's equivalent of Python's
        str.replace() - but it runs INSIDE the database (as SQL), so it
        can be applied to a column, not just a Python string. We chain
        several Replace() calls, each stripping one more formatting
        character, feeding each result into the next:

            Replace(Replace(Replace(F("phone_number"), "-", ""), "(", ""), ")", "")

        This is done once per query as part of the SQL itself - no data
        is changed in the actual database, it's purely a transformation
        applied on the fly for comparison purposes.
    """
    expression = F(field_name)
    for character in [" ", "-", "(", ")", "+", "."]:
        expression = Replace(expression, Value(character), Value(""))
    return expression


class MemberFilter(django_filters.FilterSet):
    """
    Exact-match filters: membership_type, gender, status.

    Forgiving/partial-match filters: name, membership_no, email each do
    a tokenized, punctuation-and-order-insensitive match; phone_number
    does a digits-only match that ignores formatting characters. All of
    these can be combined in one request (AND), e.g.
    ?name=jane&email=doe -> name matches "jane" AND email matches
    "doe". Omit any of these to not filter on that field.

    created_at supports a date range via
    ?created_at_after=&created_at_before= (both optional, either can be
    used alone).
    """

    # --- Forgiving text matches -------------------------------------------------
    # method= hands control to the named method below instead of using a
    # plain lookup_expr, which is what lets us plug in tokenized
    # matching instead of a single strict `icontains`.
    name = django_filters.CharFilter(method="filter_name")
    email = django_filters.CharFilter(method="filter_email")
    membership_no = django_filters.CharFilter(method="filter_membership_no")

    def filter_name(self, queryset, name, value):
        return filter_by_tokens(queryset, "name", value)

    def filter_email(self, queryset, name, value):
        return filter_by_tokens(queryset, "email", value)

    def filter_membership_no(self, queryset, name, value):
        return filter_by_tokens(queryset, "membership_no", value)

    # --- Phone number (digits-only match) ----------------------------------------
    def filter_phone_number(self, queryset, name, value):
        # Strip formatting characters from the SUBMITTED query the same
        # way we strip them from the stored column below, so both sides
        # of the comparison are in the same "digits-only" shape.
        digits_only_query = re.sub(r"[^\d]", "", value)

        if not digits_only_query:
            # Nothing left to search for once punctuation is stripped
            # (e.g. someone submitted just "--") - skip filtering rather
            # than matching everything or raising an error.
            return queryset

        # .annotate() adds a computed column to each row for the
        # duration of this query - here, the phone_number value with
        # formatting characters stripped out - which we can then filter
        # on just like any real field.
        queryset = queryset.annotate(
            _normalized_phone=normalized_digits_expression("phone_number")
        )
        return queryset.filter(_normalized_phone__icontains=digits_only_query)

    phone_number = django_filters.CharFilter(method="filter_phone_number")

    # --- created_at range -------------------------------------------------------
    created_at_after = django_filters.DateFilter(field_name="created_at", lookup_expr="gte")
    created_at_before = django_filters.DateFilter(field_name="created_at", lookup_expr="lte")

    class Meta:
        model = Member
        # membership_type, gender, and status stay as auto-generated
        # EXACT matches (django-filter's default for fields listed here
        # without a custom Filter declared above) - these are fixed
        # categories, so exact matching is the correct behavior, same
        # reasoning as `genre` on the Book model.
        fields = ["membership_type", "gender", "status"]