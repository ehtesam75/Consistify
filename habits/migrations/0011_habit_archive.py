# Generated migration for adding archive fields to Habit

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("habits", "0010_plan_version_quantity_fields"),
    ]

    operations = [
        migrations.AddField(
            model_name="habit",
            name="archived_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="habit",
            name="archive_effective_date",
            field=models.DateField(blank=True, null=True),
        ),
    ]
