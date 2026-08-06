# Generated migration for adding quantity fields to HabitPlanVersion

from django.db import migrations, models



def backfill_quantity_fields(apps, schema_editor):
    """Backfill ``habit_type`` / ``target_value`` / ``unit`` on the initial plan
    for every habit that already has one.
    """
    Habit = apps.get_model("habits", "Habit")
    HabitPlanVersion = apps.get_model("habits", "HabitPlanVersion")
    database = schema_editor.connection.alias

    for habit in Habit.objects.using(database).all().iterator(chunk_size=500):
        initial_plan = (
            HabitPlanVersion.objects.using(database)
            .filter(habit=habit)
            .order_by("effective_from", "id")
            .first()
        )
        if initial_plan is None:
            continue

        updates = {}
        if habit.habit_type and initial_plan.habit_type != habit.habit_type:
            updates["habit_type"] = habit.habit_type
        if habit.target_value is not None and initial_plan.target_value != habit.target_value:
            updates["target_value"] = habit.target_value
        if habit.unit and initial_plan.unit != habit.unit:
            updates["unit"] = habit.unit

        if updates:
            HabitPlanVersion.objects.using(database).filter(pk=initial_plan.pk).update(
                **updates
            )


class Migration(migrations.Migration):

    dependencies = [
        ("habits", "0009_habit_plan_version"),
    ]

    operations = [
        migrations.AddField(
            model_name="habitplanversion",
            name="habit_type",
            field=models.CharField(
                choices=[
                    ("binary", "Binary (Done / Not Done)"),
                    ("partial", "Partial (0-100%)"),
                    ("quantitative", "Quantitative (Target value)"),
                ],
                default="binary",
                max_length=14,
            ),
        ),
        migrations.AddField(
            model_name="habitplanversion",
            name="target_value",
            field=models.DecimalField(
                blank=True,
                decimal_places=2,
                max_digits=8,
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="habitplanversion",
            name="unit",
            field=models.CharField(blank=True, max_length=24),
        ),
        migrations.RunPython(
            backfill_quantity_fields,
            migrations.RunPython.noop,
        ),
    ]
