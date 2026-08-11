"""Stop plan versions from silently defaulting to a binary habit type.

``0010`` added ``habit_type`` to ``HabitPlanVersion`` with ``default="binary"``
and backfilled only the *first* plan row of each habit. Every later row that
already existed kept the column default, so a quantitative habit that had been
edited before the upgrade resolved as **binary** on every date-scoped page
(Today, history, reports) while Edit Habit still read the untouched ``Habit``
record and showed it as quantitative.

This migration removes the misleading default and repairs the rows that the
original backfill missed.
"""

from django.db import migrations, models
from django.db.migrations.recorder import MigrationRecorder


def repair_missed_backfill(apps, schema_editor):
    """Copy the initial plan's quantity fields onto pre-0010 sibling rows.

    Only rows that provably predate ``0010`` are touched. ``habit_type`` became
    versionable in that migration, so before it ran a habit had exactly one
    type for its whole history -- the value ``0010`` backfilled onto the
    initial plan row. Copying that value onto the habit's other pre-0010 rows
    therefore restores what those rows always meant.

    Rows created after ``0010`` are deliberately left alone: a user is allowed
    to change habit type, and those rows carry a type that was explicitly
    written by an edit. Guessing at them would overwrite a real intent.
    """
    HabitPlanVersion = apps.get_model("habits", "HabitPlanVersion")
    database = schema_editor.connection.alias

    record = (
        MigrationRecorder(schema_editor.connection)
        .migration_qs.filter(
            app="habits",
            name="0010_plan_version_quantity_fields",
        )
        .first()
    )
    if record is None:
        # 0010 is being applied in this same run (a new database), so there is
        # no pre-existing data that could have missed its backfill.
        return

    cutoff = record.applied
    stale_rows = HabitPlanVersion.objects.using(database).filter(created_at__lt=cutoff)
    habit_ids = sorted(set(stale_rows.values_list("habit_id", flat=True)))

    for habit_id in habit_ids:
        plans = list(
            HabitPlanVersion.objects.using(database)
            .filter(habit_id=habit_id)
            .order_by("effective_from", "id")
        )
        if len(plans) < 2:
            continue

        initial_plan = plans[0]
        for plan in plans[1:]:
            if plan.created_at >= cutoff:
                continue
            if (
                plan.habit_type == initial_plan.habit_type
                and plan.target_value == initial_plan.target_value
                and plan.unit == initial_plan.unit
            ):
                continue
            HabitPlanVersion.objects.using(database).filter(pk=plan.pk).update(
                habit_type=initial_plan.habit_type,
                target_value=initial_plan.target_value,
                unit=initial_plan.unit,
            )


class Migration(migrations.Migration):

    dependencies = [
        ("habits", "0012_dailyrecapcompletion"),
    ]

    operations = [
        migrations.AlterField(
            model_name="habitplanversion",
            name="habit_type",
            field=models.CharField(
                blank=True,
                choices=[
                    ("binary", "Binary (Done / Not Done)"),
                    ("partial", "Partial (0-100%)"),
                    ("quantitative", "Quantitative (Target value)"),
                ],
                max_length=14,
            ),
        ),
        migrations.RunPython(
            repair_missed_backfill,
            migrations.RunPython.noop,
        ),
    ]
