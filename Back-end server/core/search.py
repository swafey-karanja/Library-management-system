"""
core/search.py

Shared search helpers used across multiple apps (books, members, users,
...). Pulled out into `core` once the same "tokenized, punctuation- and
order-insensitive, partial match" pattern started showing up in a third
app - rather than copy-pasting the same two functions into every app's
filters.py/views.py.

WHERE THIS IS USED:
    - books/filters.py    (title, subtitle, authors, isbn_10, isbn_13)
    - members/filters.py  (name, email, membership_no)
    - apps/users/views.py (name, email)

If you refactor books/filters.py or members/filters.py later, they can
import `tokenize`/`filter_by_tokens` from here instead of defining their
own local copies, so there's only one place to fix if this logic ever
needs to change.
"""

import re


def tokenize(value):
    """
    Breaks a search string into individual "words", discarding any
    punctuation or whitespace between them.

    WHY THIS EXISTS:
        A plain `icontains` lookup checks for one EXACT literal
        substring, including punctuation and word order. That's too
        strict for real-world input variance - e.g. a client searching
        "Doe Jane" or "Doe, Jane" or "Jane Doe" should all find the same
        person, but a single icontains would only match whichever exact
        spacing/order/punctuation happens to be stored.

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