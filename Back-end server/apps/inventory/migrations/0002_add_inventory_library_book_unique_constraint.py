from django.db import migrations, models


class Migration(migrations.Migration):

    # Adjust this to whatever your last real migration for this app is
    # (e.g. the one that first registered the Inventory model's state).
    dependencies = [
        ("inventory", "0001_initial"),
    ]

    operations = [
        # AddConstraint is a normal Django migration operation. When you
        # run `migrate`, Django's schema editor translates this into the
        # correct ALTER TABLE ... ADD CONSTRAINT ... UNIQUE (...) SQL for
        # PostgreSQL and executes it for you -- you never type SQL yourself.
        migrations.AddConstraint(
            model_name="inventory",
            constraint=models.UniqueConstraint(
                fields=["library", "book"],
                name="uq_inventory_library_book",
            ),
        ),
    ]