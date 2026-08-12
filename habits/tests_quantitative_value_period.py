"""Regression cover for the Average Daily Value averaging period.

The Habit Details page shows an **Average Daily Value** for quantitative
habits. A quantitative reading only has a fixed meaning while the target, the
unit, and the habit type stay the same: "5" means something different once the
target changes from 10 to 20, or the unit changes from glasses to litres, or
the habit only just became quantitative. Averaging readings from either side of
such a change blends incomparable numbers.

The averaging period therefore restarts on the *latest effective* plan change
that alters what the quantitative value means:

* the target value changed,
* the unit changed, or
* the habit type changed into (or restarted) a quantitative configuration.

Schedule, priority, category, and name changes never restart it. When no
relevant change has ever occurred the habit's original tracking start is used,
so the full history is averaged exactly as before. The effective-dated
``HabitPlanVersion`` history is the source of truth, and the finalized-analytics
rule still applies: only changes and data through *yesterday* are counted.

These tests pin ``self.today`` to ``timezone.localdate()`` (like the existing
plan-version regression suites) so the analytics cutoff of yesterday is
deterministic without freezing the clock.
"""

from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import DEFAULT_CATEGORIES, Habit, HabitCategory, HabitCompletion
from .plan_versions import ensure_initial_plan_version, schedule_habit_plan_edit
from .services import (
    completion_stats,
    habit_tracking_start,
    quantitative_value_period_start,
)


class QuantitativeValuePeriodTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="value-period-user",
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
        # A comfortably long history so every scheduled edit lands well before
        # the finalized cutoff (yesterday) unless a test deliberately places it
        # on today.
        self.start = self.today - timedelta(days=40)

    # -- helpers ---------------------------------------------------------

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

    def _make_partial(self, name="Read", **overrides):
        fields = {
            "user": self.user,
            "name": name,
            "habit_type": Habit.HABIT_PARTIAL,
            "schedule_type": Habit.SCHEDULE_DAILY,
            "priority": Habit.PRIORITY_MEDIUM,
            "start_date": self.start,
        }
        fields.update(overrides)
        habit = Habit.objects.create(**fields)
        habit.categories.set([self.categories["study"]])
        ensure_initial_plan_version(habit)
        return habit

    def _reloaded(self, habit):
        return Habit.objects.prefetch_related(
            "pauses", "plan_versions__categories"
        ).get(pk=habit.pk)

    def _log(self, habit, target_date, raw_value, percentage=100):
        completion, _ = HabitCompletion.objects.get_or_create(
            habit=habit,
            date=target_date,
        )
        completion.completion_percentage = Decimal(str(percentage))
        completion.raw_value = Decimal(str(raw_value))
        completion.save(update_fields=["completion_percentage", "raw_value"])
        return completion

    # -- period-start rule ----------------------------------------------

    def test_no_plan_changes_uses_tracking_start(self):
        habit = self._reloaded(self._make_quantitative())
        self.assertEqual(
            quantitative_value_period_start(habit, self.today),
            habit_tracking_start(habit),
        )

    def test_target_change_restarts_period(self):
        habit = self._make_quantitative()
        edit_day = self.start + timedelta(days=9)
        schedule_habit_plan_edit(
            habit, target_value=Decimal("15"), today=edit_day
        )

        habit = self._reloaded(habit)
        self.assertEqual(
            quantitative_value_period_start(habit, self.today),
            edit_day + timedelta(days=1),
        )

    def test_unit_change_restarts_period(self):
        habit = self._make_quantitative()
        edit_day = self.start + timedelta(days=9)
        schedule_habit_plan_edit(habit, unit="cups", today=edit_day)

        habit = self._reloaded(habit)
        self.assertEqual(
            quantitative_value_period_start(habit, self.today),
            edit_day + timedelta(days=1),
        )

    def test_habit_type_change_into_quantitative_restarts_period(self):
        habit = self._make_partial()
        edit_day = self.start + timedelta(days=9)
        schedule_habit_plan_edit(
            habit,
            habit_type=Habit.HABIT_QUANTITATIVE,
            target_value=Decimal("10"),
            unit="glasses",
            today=edit_day,
        )

        habit = self._reloaded(habit)
        self.assertEqual(
            quantitative_value_period_start(habit, self.today),
            edit_day + timedelta(days=1),
        )

    def test_schedule_only_change_does_not_reset(self):
        habit = self._make_quantitative()
        edit_day = self.start + timedelta(days=9)
        schedule_habit_plan_edit(
            habit,
            schedule_type=Habit.SCHEDULE_DAYS,
            days_of_week="0,1,2,3,4,5,6",
            today=edit_day,
        )

        habit = self._reloaded(habit)
        self.assertEqual(
            quantitative_value_period_start(habit, self.today),
            habit_tracking_start(habit),
        )

    def test_priority_category_and_name_changes_do_not_reset(self):
        habit = self._make_quantitative()
        edit_day = self.start + timedelta(days=9)
        schedule_habit_plan_edit(
            habit,
            priority=Habit.PRIORITY_HIGH,
            categories=[self.categories["spiritual"]],
            today=edit_day,
        )
        # Renaming is not a plan-versioned field, so it can never move the
        # period; changing it here proves the reader ignores it.
        habit = self._reloaded(habit)
        habit.name = "Hydrate more"
        habit.save(update_fields=["name"])

        habit = self._reloaded(habit)
        self.assertEqual(
            quantitative_value_period_start(habit, self.today),
            habit_tracking_start(habit),
        )

    def test_multiple_relevant_changes_use_latest_effective_date(self):
        habit = self._make_quantitative()
        first_edit = self.start + timedelta(days=5)
        second_edit = self.start + timedelta(days=12)
        schedule_habit_plan_edit(
            habit, target_value=Decimal("15"), today=first_edit
        )
        schedule_habit_plan_edit(habit, unit="cups", today=second_edit)

        habit = self._reloaded(habit)
        self.assertEqual(
            quantitative_value_period_start(habit, self.today),
            second_edit + timedelta(days=1),
        )

    def test_irrelevant_change_between_relevant_ones_keeps_latest_relevant(self):
        habit = self._make_quantitative()
        target_edit = self.start + timedelta(days=6)
        schedule_habit_plan_edit(
            habit, target_value=Decimal("15"), today=target_edit
        )
        # A later schedule-only edit must not push the period past the last
        # change that actually altered the value's meaning.
        schedule_habit_plan_edit(
            habit,
            schedule_type=Habit.SCHEDULE_INTERVAL,
            interval_days=2,
            today=self.start + timedelta(days=20),
        )

        habit = self._reloaded(habit)
        self.assertEqual(
            quantitative_value_period_start(habit, self.today),
            target_edit + timedelta(days=1),
        )

    # -- finalized-through-yesterday behaviour --------------------------

    def test_change_effective_today_is_not_yet_counted(self):
        """A change effective *today* is not finalized, so it must not reset.

        The finalized-analytics rule only counts data (and changes) through
        yesterday, so a target change that takes effect today has no finalized
        history under it yet and cannot truncate the averaging period.
        """
        habit = self._make_quantitative()
        # ``schedule_habit_plan_edit`` makes the change effective tomorrow, so
        # editing "yesterday" lands the change on today.
        schedule_habit_plan_edit(
            habit,
            target_value=Decimal("15"),
            today=self.today - timedelta(days=1),
        )

        habit = self._reloaded(habit)
        self.assertEqual(
            quantitative_value_period_start(habit, self.today),
            habit_tracking_start(habit),
        )

    def test_change_effective_yesterday_is_counted(self):
        habit = self._make_quantitative()
        # Effective tomorrow relative to "two days ago" == yesterday.
        schedule_habit_plan_edit(
            habit,
            target_value=Decimal("15"),
            today=self.today - timedelta(days=2),
        )

        habit = self._reloaded(habit)
        self.assertEqual(
            quantitative_value_period_start(habit, self.today),
            self.today - timedelta(days=1),
        )

    # -- end-to-end average value ---------------------------------------

    def test_average_value_only_uses_readings_from_current_period(self):
        habit = self._make_quantitative()
        # The daily "Average Daily Value" divides the summed value by every
        # scheduled day in the window (an unlogged day counts as 0), so this
        # places the target change near the end and logs every finalized day
        # on each side. The old target-10 readings then sit entirely outside
        # the restarted period and cannot be blended into the new average.
        edit_day = self.today - timedelta(days=4)
        for offset in (8, 7, 6):
            self._log(habit, self.today - timedelta(days=offset), raw_value=10)

        schedule_habit_plan_edit(
            habit, target_value=Decimal("20"), today=edit_day
        )
        # New plan (target 20) is effective edit_day + 1 == today - 3. Log
        # every finalized day it schedules (today-3, today-2, today-1).
        for offset in (3, 2, 1):
            self._log(habit, self.today - timedelta(days=offset), raw_value=20)

        habit = self._reloaded(habit)
        period_start = quantitative_value_period_start(habit, self.today)
        self.assertEqual(period_start, edit_day + timedelta(days=1))

        stats = completion_stats(habit, period_start, self.today)
        # Only the post-change readings (all 20) may be averaged; the old
        # target-10 readings are a different measurement and are excluded.
        self.assertTrue(stats["average_value_is_quantitative"])
        self.assertFalse(stats["average_value_spans_mixed_plans"])
        self.assertEqual(stats["average_value"], 20)

    def test_average_value_excludes_today_even_within_period(self):
        # A one-day-old habit keeps the averaging window to a single finalized
        # day, so the daily average is decided purely by which readings enter
        # analytics rather than by the divisor.
        habit = self._reloaded(
            self._make_quantitative(start_date=self.today - timedelta(days=1))
        )
        period_start = quantitative_value_period_start(habit, self.today)
        self.assertEqual(period_start, self.today - timedelta(days=1))

        # A finalized reading (yesterday) plus a live reading (today). Today
        # must never enter analytics, so only yesterday's value is averaged.
        self._log(habit, self.today - timedelta(days=1), raw_value=8)
        self._log(habit, self.today, raw_value=2)

        stats = completion_stats(habit, period_start, self.today)
        self.assertEqual(stats["average_value"], 8)


    # -- "Since" date on the page ---------------------------------------

    def test_detail_since_date_matches_average_value_period(self):
        habit = self._make_quantitative()
        edit_day = self.start + timedelta(days=9)
        schedule_habit_plan_edit(
            habit, target_value=Decimal("15"), today=edit_day
        )

        reloaded = self._reloaded(habit)
        expected_start = quantitative_value_period_start(reloaded, self.today)
        expected_label = f"Since {expected_start.strftime('%b %d, %Y')}"

        self.client.force_login(self.user)
        response = self.client.get(
            reverse("habits:habit_detail", args=[habit.pk])
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["avg_value_label"], expected_label)
        # The stat block on the page must advertise that exact date.
        self.assertContains(response, expected_label)

    def test_detail_since_date_is_tracking_start_without_relevant_changes(self):
        habit = self._make_quantitative()
        reloaded = self._reloaded(habit)
        expected_label = (
            f"Since {habit_tracking_start(reloaded).strftime('%b %d, %Y')}"
        )

        self.client.force_login(self.user)
        response = self.client.get(
            reverse("habits:habit_detail", args=[habit.pk])
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["avg_value_label"], expected_label)
