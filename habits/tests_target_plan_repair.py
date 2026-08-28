"""Regression tests for stale quantitative targets on date-scoped pages."""

import importlib
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

from django.apps import apps
from django.contrib import admin
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase
from django.utils import timezone

from .admin import HabitAdmin
from .models import Habit, HabitCategory, HabitPlanVersion
from .plan_versions import ensure_initial_plan_version, schedule_habit_plan_edit
from .services import compute_today_metrics, resolve_habit_plan_on


class StaleTargetPlanRepairTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="target-repair-user",
            password="not-used",
        )
        self.category, _ = HabitCategory.objects.get_or_create(
            key="health",
            defaults={"label": "Health", "sort_order": 1},
        )
        self.today = timezone.localdate()
        self.start = self.today - timedelta(days=20)

    def _make_habit(self):
        habit = Habit.objects.create(
            user=self.user,
            name="Water",
            habit_type=Habit.HABIT_QUANTITATIVE,
            target_value=Decimal("10"),
            unit="glasses",
            schedule_type=Habit.SCHEDULE_DAILY,
            priority=Habit.PRIORITY_MEDIUM,
            start_date=self.start,
        )
        habit.categories.set([self.category])
        ensure_initial_plan_version(habit)
        return habit

    def _reload(self, habit):
        return Habit.objects.prefetch_related(
            "pauses", "plan_versions__categories"
        ).get(pk=habit.pk)

    def _today_target(self, habit, target_date):
        metrics = compute_today_metrics(self.user, target_date)
        row = next(row for row in metrics["rows"] if row["habit"].pk == habit.pk)
        return row["target_value"], row["unit"]

    def _run_repair_migration(self):
        migration = importlib.import_module(
            "habits.migrations.0014_repair_stale_quantity_plan_mirrors"
        )
        migration.repair_stale_quantity_plan_mirrors(
            apps,
            SimpleNamespace(connection=connection),
        )

    def test_admin_cannot_bypass_quantity_plan_versioning(self):
        habit = self._make_habit()
        model_admin = HabitAdmin(Habit, admin.site)

        readonly = model_admin.get_readonly_fields(request=None, obj=habit)

        self.assertIn("habit_type", readonly)
        self.assertIn("target_value", readonly)
        self.assertIn("unit", readonly)

    def test_repair_makes_today_use_the_current_target_again(self):
        habit = self._make_habit()

        # Reproduce the old admin/direct-write corruption: Habit changes but no
        # effective-dated plan row is written. Edit Habit would read 25/cups,
        # while Today still resolves the old 10/glasses plan.
        Habit.objects.filter(pk=habit.pk).update(
            target_value=Decimal("25"),
            unit="cups",
        )
        habit = self._reload(habit)
        self.assertEqual(habit.target_value, Decimal("25"))
        self.assertEqual(self._today_target(habit, self.today), (Decimal("10"), "glasses"))

        self._run_repair_migration()

        habit = self._reload(habit)
        self.assertEqual(
            self._today_target(habit, self.today),
            (Decimal("25"), "cups"),
        )
        self.assertEqual(
            self._today_target(habit, self.today + timedelta(days=7)),
            (Decimal("25"), "cups"),
        )

    def test_repair_changes_only_latest_plan_and_keeps_real_old_target(self):
        habit = self._make_habit()

        # A genuine historical target change: 10 -> 15. This produces a real
        # second plan row and must remain intact for dates before that row.
        change_day = self.today - timedelta(days=8)
        schedule_habit_plan_edit(
            habit,
            target_value=Decimal("15"),
            today=change_day,
        )
        effective = change_day + timedelta(days=1)

        # Later, reproduce the bypass that changed only the Habit mirror.
        Habit.objects.filter(pk=habit.pk).update(
            target_value=Decimal("25"),
            unit="cups",
        )
        habit = self._reload(habit)

        old_config = resolve_habit_plan_on(habit, effective - timedelta(days=1))
        stale_current = resolve_habit_plan_on(habit, self.today)
        self.assertEqual(old_config.target_value, Decimal("10"))
        self.assertEqual(stale_current.target_value, Decimal("15"))

        self._run_repair_migration()

        habit = self._reload(habit)
        old_config = resolve_habit_plan_on(habit, effective - timedelta(days=1))
        current_config = resolve_habit_plan_on(habit, self.today)
        latest = habit.plan_versions.order_by("-effective_from", "-id").first()
        earliest = habit.plan_versions.order_by("effective_from", "id").first()

        self.assertEqual(old_config.target_value, Decimal("10"))
        self.assertEqual(earliest.target_value, Decimal("10"))
        self.assertEqual(current_config.target_value, Decimal("25"))
        self.assertEqual(current_config.unit, "cups")
        self.assertEqual(latest.target_value, Decimal("25"))
        self.assertEqual(latest.unit, "cups")

    def test_repair_is_idempotent(self):
        habit = self._make_habit()
        Habit.objects.filter(pk=habit.pk).update(target_value=Decimal("25"))

        self._run_repair_migration()
        first = list(
            HabitPlanVersion.objects.filter(habit=habit)
            .order_by("effective_from", "id")
            .values_list("habit_type", "target_value", "unit")
        )
        self._run_repair_migration()
        second = list(
            HabitPlanVersion.objects.filter(habit=habit)
            .order_by("effective_from", "id")
            .values_list("habit_type", "target_value", "unit")
        )

        self.assertEqual(first, second)
