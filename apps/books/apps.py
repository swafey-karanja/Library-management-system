from django.apps import AppConfig


class BooksConfig(AppConfig):
    # Must match the actual dotted import path to this app, since it
    # lives inside the 'apps' package (apps/libraries/) rather than at
    # the project root. Django uses this to resolve the app during
    # INSTALLED_APPS loading and to determine the app_label for models.
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.books"
    label = "books"
