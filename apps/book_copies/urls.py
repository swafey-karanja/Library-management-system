"""
urls.py — book_copies app routes
===================================

This file maps URL paths to the views defined in views.py, scoped to
this specific app. Keeping a urls.py PER APP (rather than dumping every
route into the project's main urls.py) keeps things modular — each
app owns its own routes, and the project just "includes" them.
"""

from django.urls import path

from .views import BookCopyListView

# `app_name` sets a "namespace" for these URLs. This matters once your
# project has many apps that might reuse the same view name (e.g. both
# `books` and `book_copies` might have a view called "list") — with a
# namespace, you can always refer to this one unambiguously elsewhere
# in Django as 'book_copies:list'.
app_name = 'book_copies'

urlpatterns = [
    path('', BookCopyListView.as_view(), name='list'),
]