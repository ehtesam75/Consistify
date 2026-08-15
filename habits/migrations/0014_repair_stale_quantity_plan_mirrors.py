"""Repair quantity plan rows that diverged from the mirrored Habit record.

The normal Edit Habit flow writes a ``HabitPlanVersion`` and then mirrors the
newest configuration onto ``Habit``. Older Django-admin behaviour allowed
``habit_type``, ``target_value`` and ``unit`` to be edited directly on Habit,
which bypassed version creation. The edit form therefore showed the new Habit
values while Today/history/future pages kept resolving the stale latest plan.

Only the newest plan row is repairable without guessing at genuine historical
changes. Earlier rows remain untouched so legitimate old targets are preserved.
"""

from django.db import migrations


QUANTITATIVE = "quantitative"


def repair_stale_quantity_plan_mirrors(apps, schema_editor):
    Habit = apps.get_model("habits", "Habit")
    HabitPlanVersion = apps.get_model("habits", "HabitPlanVersion")
    database = schema_editor.connection.alias

    for habit in Habit.objects.using(database).all().iterator(chunk_size=500):
        latest = (
            HabitPlanVersion.objects.using(database)
            .filter(habit_id=habit.pk)
            .order_by("-effective_from", "-id")
            .first()
        )
        if latest is None:
            continue

        habit_type = habit.habit_type
        target_value = habit.target_value if habit_type == QUANTITATIVE else None
        unit = (habit.unit or "") if habit_type == QUANTITATIVE else ""

        if (
            latest.habit_type == habit_type
            and latest.target_value == target_value
            and latest.unit == unit
        ):
            continue

        HabitPlanVersion.objects.using(database).filter(pk=latest.pk).update(
            habit_type=habit_type,
            target_value=target_value,
            unit=unit,
        )


class Migration(migrations.Migration):

    dependencies = [
        ("habits", "0013_plan_version_habit_type_inherit"),
    ]

    operations = [
        migrations.RunPython(
            repair_stale_quantity_plan_mirrors,
            migrations.RunPython.noop,
        ),
    ]
