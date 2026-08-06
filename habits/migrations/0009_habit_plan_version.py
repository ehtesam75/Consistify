import django.db.models.deletion
from django.db import migrations, models
from django.utils import timezone


def backfill_habit_plan_versions(apps, schema_editor):
    Habit = apps.get_model("habits", "Habit")
    HabitPlanVersion = apps.get_model("habits", "HabitPlanVersion")
    database = schema_editor.connection.alias

    for habit in Habit.objects.using(database).all().iterator(chunk_size=500):
        if timezone.is_aware(habit.created_at):
            created_date = timezone.localtime(habit.created_at).date()
        else:
            created_date = habit.created_at.date()
        version = HabitPlanVersion.objects.using(database).create(
            habit_id=habit.pk,
            effective_from=min(habit.start_date, created_date),
            schedule_anchor=habit.start_date,
            schedule_type=habit.schedule_type,
            interval_days=max(1, habit.interval_days or 1),
            weekly_interval=max(1, habit.weekly_interval or 1),
            days_of_week=habit.days_of_week or "",
            priority=habit.priority,
        )
        category_ids = list(
            habit.categories.using(database).values_list("pk", flat=True)
        )
        version.categories.set(category_ids)


class Migration(migrations.Migration):

    dependencies = [
        ("habits", "0008_update_habit_categories"),
    ]

    operations = [
        migrations.CreateModel(
            name="HabitPlanVersion",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("effective_from", models.DateField()),
                ("schedule_anchor", models.DateField()),
                (
                    "schedule_type",
                    models.CharField(
                        choices=[
                            ("daily", "Daily"),
                            ("weekly", "Weekly"),
                            ("days", "Specific days"),
                            ("interval", "Custom interval"),
                        ],
                        max_length=12,
                    ),
                ),
                ("interval_days", models.PositiveSmallIntegerField(default=1)),
                ("weekly_interval", models.PositiveSmallIntegerField(default=1)),
                ("days_of_week", models.CharField(blank=True, max_length=20)),
                (
                    "priority",
                    models.CharField(
                        choices=[
                            ("high", "High"),
                            ("medium", "Medium"),
                            ("low", "Low"),
                        ],
                        default="medium",
                        max_length=6,
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "categories",
                    models.ManyToManyField(
                        blank=True,
                        related_name="habit_plan_versions",
                        to="habits.habitcategory",
                    ),
                ),
                (
                    "habit",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="plan_versions",
                        to="habits.habit",
                    ),
                ),
            ],
            options={
                "ordering": ["effective_from", "id"],
                "indexes": [
                    models.Index(
                        fields=["habit", "effective_from"],
                        name="habit_plan_effective_idx",
                    ),
                ],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("habit", "effective_from"),
                        name="habit_plan_effective_unique",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(("interval_days__gte", 1)),
                        name="habit_plan_interval_gte_1",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(("weekly_interval__gte", 1)),
                        name="habit_plan_weekly_gte_1",
                    ),
                ],
            },
        ),
        migrations.RunPython(
            backfill_habit_plan_versions,
            migrations.RunPython.noop,
        ),
    ]
