"""Regression cover for quantitative habits silently becoming binary.

Migration 0010 added ``habit_type`` to ``HabitPlanVersion`` with
``default="binary"`` and backfilled only each habit's *first* plan row. Any
later row kept the column default, so date-scoped pages (Today, history,
reports) resolved the habit as binary while Edit Habit still read the
untouched ``Habit`` record and showed quantitative.

These tests assert the invariant from both directions: every writer must
persist a real type, and every reader must resolve the same effective type as
Edit Habit for the same date.
"""

from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import DEFAULT_CATEGORIES, Habit, HabitCategory, HabitPlanVersion
from .plan_versions import ensure_initial_plan_version, schedule_habit_plan_edit
from .services import (
    compute_today_metrics,
    get_pending_habits_for_date,
    habit_performance_metrics,
    resolve_habit_plan_on,
    scheduled_occurrence_on,
)


class QuantitativeHabitTypePreservationTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="quant-user",
            password="not-used",
        )
        self.categories = {}
        for index, (key, label) in enumerate(DEFAULT_CATEGORIES, start=1):
            category, _ = HabitCategory.objects.get_or_create(
                key=key,
                defaults={"label": label, "sort_order": index},
            )
            self.categories[key] = category

        self.today = timezone.localdate()
        self.start = self.today - timedelta(days=30)

    def _make_quantitative(self, name="Drink water", **overrides):
        fields = {
            "user": self.user,
            "name": name,
            "habit_type": Habit.HABIT_QUANTITATIVE,
            "target_value": Decimal("10"),
            "unit": "glasses",
            "schedule_type": Habit.SCHEDULE_DAILY,
            "priority": Habit.PRIORITY_MEDIUM,
            "start_date": self.start,
        }
        fields.update(overrides)
        habit = Habit.objects.create(**fields)
        habit.categories.set([self.categories["health"]])
        ensure_initial_plan_version(habit)
        return habit

    def _reloaded(self, habit):
        return Habit.objects.prefetch_related(
            "pauses", "plan_versions__categories"
        ).get(pk=habit.pk)

    def _assert_quantitative_everywhere(
        self, habit, target_date, msg, require_stored_type=True
    ):
        """Assert every read path agrees the habit is quantitative on a date.

        ``require_stored_type`` is disabled only by the legacy-corruption test,
        which forces a blank column value on disk to prove the *reader* stays
        correct even for rows written before the fix.
        """
        habit = self._reloaded(habit)


        # Edit Habit reads the base record.
        self.assertEqual(habit.habit_type, Habit.HABIT_QUANTITATIVE, msg)

        # Today / history / reports read the effective-dated plan.
        config = resolve_habit_plan_on(habit, target_date)
        self.assertIsNotNone(config, msg)
        self.assertEqual(config.habit_type, Habit.HABIT_QUANTITATIVE, msg)
        self.assertTrue(config.is_quantitative, msg)

        # The scheduled occurrence carries the same configuration.
        occurrence = scheduled_occurrence_on(habit, target_date)
        self.assertIsNotNone(occurrence, msg)
        self.assertEqual(
            occurrence.config.habit_type, Habit.HABIT_QUANTITATIVE, msg
        )

        # Edit Habit and the date-scoped pages must never disagree.
        self.assertEqual(habit.habit_type, config.habit_type, msg)

        # No stored plan row may carry a blank/defaulted type.
        if require_stored_type:
            for version in habit.plan_versions.all():
                self.assertTrue(version.habit_type, msg)


    def test_edit_priority_only_keeps_habit_quantitative(self):
        habit = self._make_quantitative()
        schedule_habit_plan_edit(
            habit, priority=Habit.PRIORITY_HIGH, today=self.today
        )

        effective = self.today + timedelta(days=1)
        self._assert_quantitative_everywhere(
            habit, effective, "priority-only edit reclassified the habit"
        )
        config = resolve_habit_plan_on(self._reloaded(habit), effective)
        self.assertEqual(config.priority, Habit.PRIORITY_HIGH)
        self.assertEqual(config.target_value, Decimal("10"))
        self.assertEqual(config.unit, "glasses")

    def test_edit_target_only_keeps_habit_quantitative(self):
        habit = self._make_quantitative()
        schedule_habit_plan_edit(habit, target_value=Decimal("15"), today=self.today)

        effective = self.today + timedelta(days=1)
        self._assert_quantitative_everywhere(
            habit, effective, "target-only edit reclassified the habit"
        )
        config = resolve_habit_plan_on(self._reloaded(habit), effective)
        self.assertEqual(config.target_value, Decimal("15"))
        self.assertEqual(config.unit, "glasses")

    def test_edit_schedule_and_category_keeps_habit_quantitative(self):
        habit = self._make_quantitative()
        schedule_habit_plan_edit(
            habit,
            schedule_type=Habit.SCHEDULE_DAYS,
            days_of_week="0,1,2,3,4,5,6",
            categories=[self.categories["study"]],
            today=self.today,
        )

        effective = self.today + timedelta(days=1)
        self._assert_quantitative_everywhere(
            habit, effective, "schedule/category edit reclassified the habit"
        )
        config = resolve_habit_plan_on(self._reloaded(habit), effective)
        self.assertEqual(config.schedule_type, Habit.SCHEDULE_DAYS)
        self.assertEqual(config.category_ids, frozenset({self.categories["study"].pk}))
        self.assertEqual(config.target_value, Decimal("10"))
        self.assertEqual(config.unit, "glasses")

    def test_effective_dated_edit_renders_quantitative_on_today_and_history(self):
        habit = self._make_quantitative()
        schedule_habit_plan_edit(
            habit, priority=Habit.PRIORITY_LOW, today=self.today - timedelta(days=1)
        )

        # The pending version is now active for today.
        self._assert_quantitative_everywhere(
            habit, self.today, "effective-dated edit reclassified today"
        )

        metrics = compute_today_metrics(self.user, self.today)
        row = next(row for row in metrics["rows"] if row["habit"].pk == habit.pk)
        self.assertEqual(row["habit_type"], Habit.HABIT_QUANTITATIVE)
        self.assertEqual(row["target_value"], Decimal("10"))
        self.assertEqual(row["unit"], "glasses")

        pending = get_pending_habits_for_date(self.user, self.today)
        entry = next(item for item in pending if item["habit"].pk == habit.pk)
        self.assertEqual(entry["habit_type"], Habit.HABIT_QUANTITATIVE)
        self.assertEqual(entry["unit"], "glasses")

        # A past date still resolves through the original version.
        self._assert_quantitative_everywhere(
            habit, self.start, "effective-dated edit reclassified history"
        )

    def test_multiple_consecutive_edits_preserve_quantitative_config(self):
        habit = self._make_quantitative()

        schedule_habit_plan_edit(
            habit, priority=Habit.PRIORITY_HIGH, today=self.today
        )
        schedule_habit_plan_edit(
            habit,
            schedule_type=Habit.SCHEDULE_INTERVAL,
            interval_days=2,
            today=self.today + timedelta(days=1),
        )
        schedule_habit_plan_edit(
            habit,
            categories=[self.categories["spiritual"]],
            today=self.today + timedelta(days=2),
        )

        habit = self._reloaded(habit)
        self.assertEqual(habit.habit_type, Habit.HABIT_QUANTITATIVE)
        self.assertEqual(habit.target_value, Decimal("10"))
        self.assertEqual(habit.unit, "glasses")

        for version in habit.plan_versions.all():
            self.assertEqual(version.habit_type, Habit.HABIT_QUANTITATIVE)
            self.assertEqual(version.target_value, Decimal("10"))
            self.assertEqual(version.unit, "glasses")

        final = resolve_habit_plan_on(habit, self.today + timedelta(days=3))
        self.assertEqual(final.habit_type, Habit.HABIT_QUANTITATIVE)
        self.assertEqual(final.priority, Habit.PRIORITY_HIGH)
        self.assertEqual(final.schedule_type, Habit.SCHEDULE_INTERVAL)
        self.assertEqual(final.interval_days, 2)
        self.assertEqual(
            final.category_ids, frozenset({self.categories["spiritual"].pk})
        )

    def test_edit_habit_page_and_today_agree_on_effective_type(self):
        habit = self._make_quantitative()
        self.client.force_login(self.user)

        response = self.client.post(
            reverse("habits:habit_edit", args=[habit.pk]),
            {
                "name": habit.name,
                "description": "",
                "habit_type": Habit.HABIT_QUANTITATIVE,
                "target_value": "10",
                "unit": "glasses",
                "schedule_type": Habit.SCHEDULE_DAILY,
                "categories": [self.categories["health"].pk],
                "priority": Habit.PRIORITY_HIGH,
                "tags": "",
                "interval_days": 1,
                "weekly_interval": 1,
            },
        )
        self.assertEqual(response.status_code, 302)

        habit = self._reloaded(habit)
        edit_form_type = habit.habit_type

        for offset in (-1, 0, 1, 2):
            target_date = self.today + timedelta(days=offset)
            if target_date < habit.start_date:
                continue
            config = resolve_habit_plan_on(habit, target_date)
            self.assertEqual(
                config.habit_type,
                edit_form_type,
                f"Edit Habit and date-scoped pages disagree on {target_date}",
            )

    def test_plan_version_without_type_inherits_from_habit(self):
        """A writer that omits the type must inherit it, never default to binary."""
        habit = self._make_quantitative()

        version = HabitPlanVersion.objects.create(
            habit=habit,
            effective_from=self.start + timedelta(days=5),
            schedule_anchor=self.start,
            schedule_type=Habit.SCHEDULE_DAILY,
            priority=Habit.PRIORITY_HIGH,
        )

        version.refresh_from_db()
        self.assertEqual(version.habit_type, Habit.HABIT_QUANTITATIVE)
        self.assertEqual(version.target_value, Decimal("10"))
        self.assertEqual(version.unit, "glasses")

        self._assert_quantitative_everywhere(
            habit,
            self.start + timedelta(days=6),
            "a type-less plan row resolved as binary",
        )

    def test_legacy_blank_type_row_resolves_against_the_habit(self):
        """Rows corrupted before the fix must still read as quantitative."""
        habit = self._make_quantitative()
        HabitPlanVersion.objects.create(
            habit=habit,
            effective_from=self.start + timedelta(days=5),
            schedule_anchor=self.start,
            schedule_type=Habit.SCHEDULE_DAILY,
            priority=Habit.PRIORITY_HIGH,
        )
        # Reproduce the exact on-disk corruption migration 0010 could leave.
        HabitPlanVersion.objects.filter(
            habit=habit, effective_from=self.start + timedelta(days=5)
        ).update(habit_type="", target_value=None, unit="")

        self._assert_quantitative_everywhere(
            habit,
            self.start + timedelta(days=6),
            "a legacy blank-type row resolved as binary",
            require_stored_type=False,
        )


    def test_quantitative_edit_cannot_drop_the_target_value(self):
        habit = self._make_quantitative()
        with self.assertRaises(ValidationError):
            schedule_habit_plan_edit(
                habit,
                habit_type=Habit.HABIT_QUANTITATIVE,
                target_value=Decimal("0"),
                today=self.today,
            )

    def test_habit_type_can_still_be_changed_explicitly(self):
        """The guard preserves untouched types without freezing the field."""
        habit = self._make_quantitative()
        schedule_habit_plan_edit(
            habit, habit_type=Habit.HABIT_BINARY, today=self.today
        )

        habit = self._reloaded(habit)
        self.assertEqual(habit.habit_type, Habit.HABIT_BINARY)

        config = resolve_habit_plan_on(habit, self.today + timedelta(days=1))
        self.assertEqual(config.habit_type, Habit.HABIT_BINARY)
        self.assertIsNone(config.target_value)
        self.assertEqual(config.unit, "")

        # History keeps the quantitative reading it was logged under.
        history = resolve_habit_plan_on(habit, self.start)
        self.assertEqual(history.habit_type, Habit.HABIT_QUANTITATIVE)

    def test_history_metrics_report_quantitative_average_after_unrelated_edit(self):
        habit = self._make_quantitative()
        schedule_habit_plan_edit(
            habit, priority=Habit.PRIORITY_HIGH, today=self.start
        )

        metrics = habit_performance_metrics(
            habit, self.start, self.start + timedelta(days=10)
        )
        self.assertTrue(metrics["average_value_is_quantitative"])
        self.assertEqual(metrics["average_value_unit"], "glasses")
