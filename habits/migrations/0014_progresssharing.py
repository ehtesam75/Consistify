from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("habits", "0013_plan_version_habit_type_inherit"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="ProgressSharing",
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
                (
                    "status",
                    models.CharField(
                        choices=[("pending", "Pending"), ("active", "Active")],
                        default="pending",
                        max_length=12,
                    ),
                ),
                ("requested_at", models.DateTimeField(auto_now_add=True)),
                ("accepted_at", models.DateTimeField(blank=True, null=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "friendship",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="progress_sharing",
                        to="habits.friendrequest",
                    ),
                ),
                (
                    "requester",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="requested_progress_sharing",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "user_one",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="progress_sharing_as_user_one",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "user_two",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="progress_sharing_as_user_two",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "ordering": ["-requested_at"],
                "indexes": [
                    models.Index(
                        fields=["user_one", "status"],
                        name="habits_prog_user_on_3944f4_idx",
                    ),
                    models.Index(
                        fields=["user_two", "status"],
                        name="habits_prog_user_tw_71f2c8_idx",
                    ),
                ],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("user_one", "user_two"),
                        name="progress_sharing_users_unique",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(user_one__lt=models.F("user_two")),
                        name="progress_sharing_users_ordered",
                    ),
                    models.CheckConstraint(
                        condition=(
                            models.Q(requester=models.F("user_one"))
                            | models.Q(requester=models.F("user_two"))
                        ),
                        name="progress_sharing_requester_participant",
                    ),
                    models.CheckConstraint(
                        condition=(
                            models.Q(status="pending", accepted_at__isnull=True)
                            | models.Q(status="active", accepted_at__isnull=False)
                        ),
                        name="progress_sharing_status_timestamp",
                    ),
                ],
            },
        ),
    ]
