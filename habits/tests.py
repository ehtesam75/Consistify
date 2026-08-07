import json
import os
from datetime import date, datetime, timedelta
from decimal import Decimal
from math import sqrt
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .forms import HabitForm
from .models import (
    DEFAULT_CATEGORIES,
    DailyRecapCompletion,
    FriendRequest,
    Habit,
    HabitCategory,
    HabitCompletion,
    HabitPause,
    HabitPlanVersion,
)

from .plan_versions import ensure_initial_plan_version, schedule_habit_plan_edit
from .services import (
    build_category_analytics,
    build_habit_score_drivers,
    build_monthly_reports,
    build_overall_score_breakdown,
    build_weekly_reports,
    calculate_overall_consistency,
    compute_user_metrics,
    daily_average_completion_series,
    habit_tracking_start,
    habit_performance_metrics,
    iter_scheduled_dates,
    leaderboard_ranking_score,
    completion_stats,
    get_completion_maps,
    LEADERBOARD_CONFIDENCE_SESSIONS,
)


class ConsistifyScoreAuditRegressionTests(TestCase):
    """Regression cover for the three high-priority audit fixes."""

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="audit-regression-user",
            password="not-used",
        )
        self.today = date(2026, 3, 31)
        self.category, _ = HabitCategory.objects.get_or_create(
            key=DEFAULT_CATEGORIES[0][0],
            defaults={"label": DEFAULT_CATEGORIES[0][1], "sort_order": 1},
        )

    def _make_habit(self, **overrides):
        defaults = {
            "user": self.user,
            "name": "Audit habit",
            "schedule_type": Habit.SCHEDULE_DAILY,
            "start_date": self.today - timedelta(days=9),
            "priority": Habit.PRIORITY_MEDIUM,
        }
        defaults.update(overrides)
        habit = Habit.objects.create(**defaults)
        ensure_initial_plan_version(habit)
        return habit

    # ---- H1: no duplicate average_completion metric --------------------

    def test_metrics_expose_single_canonical_completion_rate(self):
        """``average_completion`` was a literal alias of ``completion_rate``.

        Two keys for one calculation were rendered as two separate metrics in
        the UI. Only the canonical key may survive.
        """
        habit = self._make_habit()
        HabitCompletion.objects.create(
            habit=habit,
            date=self.today,
            completion_percentage=Decimal("40"),
        )

        metrics = habit_performance_metrics(
            habit,
            habit_tracking_start(habit),
            self.today,
        )
        stats = completion_stats(habit, habit_tracking_start(habit), self.today)

        self.assertNotIn("average_completion", metrics)
        self.assertNotIn("average_completion", stats)
        self.assertIn("completion_rate", metrics)
        self.assertEqual(stats["completion_rate"], metrics["completion_rate"])

    def test_habit_detail_reports_completion_rate_for_both_windows(self):
        habit = self._make_habit()
        HabitCompletion.objects.create(
            habit=habit,
            date=self.today,
            completion_percentage=Decimal("100"),
        )
        self.client.force_login(self.user)

        with patch("habits.views.timezone.localdate", return_value=self.today):
            response = self.client.get(
                reverse("habits:habit_detail", args=[habit.id])
            )

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("average_completion", response.context["stats"])
        self.assertNotIn("average_completion", response.context["all_time_stats"])
        self.assertIn("completion_rate", response.context["all_time_stats"])
        # The page must no longer label one number as a second, distinct metric.
        self.assertNotContains(response, "Avg completion")

    # ---- H2/H3: historical values keep their own plan's typing ---------

    def test_get_completion_maps_does_not_type_by_current_habit_type(self):
        """Editing habit_type must not reinterpret already-logged raw values."""
        habit = self._make_habit(
            habit_type=Habit.HABIT_QUANTITATIVE,
            target_value=Decimal("60"),
            unit="minutes",
        )
        HabitCompletion.objects.create(
            habit=habit,
            date=self.today,
            completion_percentage=Decimal("75"),
            raw_value=Decimal("45.50"),
        )

        _, value_map = get_completion_maps(habit, self.today, self.today)
        self.assertEqual(value_map[self.today], 45.5)

        # Flip the mutable current type; stored history must read identically.
        habit.habit_type = Habit.HABIT_PARTIAL
        habit.save(update_fields=["habit_type"])

        _, value_map_after = get_completion_maps(habit, self.today, self.today)
        self.assertEqual(value_map_after, value_map)

    def test_average_value_uses_effective_dated_plan_not_current_type(self):
        """History logged under a quantitative plan stays quantitative.

        Previously ``average_value`` read ``habit.habit_type``, so changing the
        type today silently rewrote how every past value was reported.
        """
        habit = self._make_habit(
            habit_type=Habit.HABIT_QUANTITATIVE,
            target_value=Decimal("60"),
            unit="minutes",
        )
        for offset in range(3):
            HabitCompletion.objects.create(
                habit=habit,
                date=self.today - timedelta(days=offset),
                completion_percentage=Decimal("50"),
                raw_value=Decimal("30"),
            )

        start = habit_tracking_start(habit)
        before = habit_performance_metrics(habit, start, self.today)
        self.assertTrue(before["average_value_is_quantitative"])
        self.assertEqual(before["average_value_unit"], "minutes")

        habit.habit_type = Habit.HABIT_PARTIAL
        habit.unit = ""
        habit.save(update_fields=["habit_type", "unit"])

        after = habit_performance_metrics(
            Habit.objects.prefetch_related("plan_versions__categories").get(
                pk=habit.pk
            ),
            start,
            self.today,
        )
        self.assertTrue(after["average_value_is_quantitative"])
        self.assertEqual(after["average_value_unit"], "minutes")
        self.assertEqual(after["average_value"], before["average_value"])

    def test_average_value_reports_latest_plan_when_units_change(self):
        """Values under incompatible units are never averaged together."""
        habit = self._make_habit(
            habit_type=Habit.HABIT_QUANTITATIVE,
            target_value=Decimal("60"),
            unit="minutes",
            start_date=self.today - timedelta(days=5),
        )
        for offset in range(3, 6):
            HabitCompletion.objects.create(
                habit=habit,
                date=self.today - timedelta(days=offset),
                completion_percentage=Decimal("100"),
                raw_value=Decimal("60"),
            )

        schedule_habit_plan_edit(
            habit,
            habit_type=Habit.HABIT_QUANTITATIVE,
            target_value=Decimal("5"),
            unit="km",
            schedule_type=Habit.SCHEDULE_DAILY,
            start_date=habit.start_date,
            interval_days=1,
            weekly_interval=1,
            days_of_week="",
            priority=Habit.PRIORITY_MEDIUM,
            categories=[self.category],
            today=self.today - timedelta(days=3),
        )
        # Log every date the new plan schedules, so the average is decided
        # purely by which plan owns the value rather than by missed sessions.
        for offset in range(0, 3):
            HabitCompletion.objects.create(
                habit=habit,
                date=self.today - timedelta(days=offset),
                completion_percentage=Decimal("100"),
                raw_value=Decimal("5"),
            )

        habit = Habit.objects.prefetch_related("plan_versions__categories").get(
            pk=habit.pk
        )
        metrics = habit_performance_metrics(
            habit,
            habit_tracking_start(habit),
            self.today,
        )

        self.assertTrue(metrics["average_value_spans_mixed_plans"])
        self.assertEqual(metrics["average_value_unit"], "km")
        # 60-minute sessions must not be blended into a kilometre average.
        self.assertEqual(metrics["average_value"], 5)

    # ---- H4: leaderboard fairness --------------------------------------

    def test_ranking_score_shrinks_thin_history_toward_neutral(self):
        thin = leaderboard_ranking_score(100.0, 3)
        full = leaderboard_ranking_score(95.0, 400)

        self.assertEqual(thin, 55.0)
        self.assertEqual(full, 95.0)
        self.assertLess(
            thin,
            full,
            "A few perfect days must not outrank sustained consistency.",
        )

    def test_ranking_score_is_unchanged_once_evidence_threshold_is_met(self):
        for score in (0.0, 42.5, 88.0, 100.0):
            self.assertEqual(
                leaderboard_ranking_score(
                    score, LEADERBOARD_CONFIDENCE_SESSIONS
                ),
                score,
            )

    def test_ranking_score_returns_zero_without_scheduled_sessions(self):
        self.assertEqual(leaderboard_ranking_score(100.0, 0), 0.0)

    def test_all_time_leaderboard_uses_one_shared_window(self):
        """Every participant must be scored over identical calendar dates."""
        veteran = self.user
        veteran_habit = self._make_habit(
            name="Veteran habit",
            start_date=self.today - timedelta(days=120),
        )
        for offset in range(120):
            HabitCompletion.objects.create(
                habit=veteran_habit,
                date=self.today - timedelta(days=offset),
                completion_percentage=Decimal("95"),
            )

        newcomer = get_user_model().objects.create_user(
            username="audit-newcomer",
            password="not-used",
        )
        newcomer_habit = Habit.objects.create(
            user=newcomer,
            name="Newcomer habit",
            schedule_type=Habit.SCHEDULE_DAILY,
            start_date=self.today - timedelta(days=2),
            priority=Habit.PRIORITY_MEDIUM,
        )
        ensure_initial_plan_version(newcomer_habit)
        for offset in range(3):
            HabitCompletion.objects.create(
                habit=newcomer_habit,
                date=self.today - timedelta(days=offset),
                completion_percentage=Decimal("100"),
            )

        FriendRequest.objects.create(
            from_user=veteran,
            to_user=newcomer,
            status=FriendRequest.STATUS_ACCEPTED,
        )

        self.client.force_login(veteran)
        with patch("habits.views.timezone.localdate", return_value=self.today):
            response = self.client.get(
                reverse("habits:leaderboard"), {"window": "all"}
            )

        self.assertEqual(response.status_code, 200)
        entries = response.context["leaderboard_entries"]
        self.assertEqual(
            response.context["leaderboard_window_start"],
            self.today - timedelta(days=120),
        )

        by_name = {entry["user"].username: entry for entry in entries}
        veteran_entry = by_name[veteran.username]
        newcomer_entry = by_name[newcomer.username]

        self.assertEqual(
            veteran_entry["rank"],
            1,
            "Sustained 95% over 120 days must outrank 3 perfect days.",
        )
        self.assertEqual(newcomer_entry["rank"], 2)
        self.assertTrue(veteran_entry["has_full_evidence"])
        self.assertFalse(newcomer_entry["has_full_evidence"])
        # The displayed Consistify Score stays the untouched raw value. Three
        # flat perfect sessions score 92.5, not 100, because an unchanging line
        # earns neutral momentum rather than an improvement bonus.
        self.assertEqual(newcomer_entry["consistency_score"], 92.5)
        # Ranking shrinks that thin record toward neutral; display does not.
        self.assertEqual(newcomer_entry["ranking_score"], 54.2)
        self.assertLess(
            newcomer_entry["ranking_score"],
            newcomer_entry["consistency_score"],
        )

    def test_current_window_leaderboard_still_uses_last_30_days(self):
        self._make_habit()
        self.client.force_login(self.user)

        with patch("habits.views.timezone.localdate", return_value=self.today):
            response = self.client.get(reverse("habits:leaderboard"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["leaderboard_window"], "current")
        self.assertEqual(
            response.context["leaderboard_window_start"],
            self.today - timedelta(days=29),
        )

    # ---- M3: score breakdown and drivers reuse precomputed metrics -----

    def test_breakdown_and_drivers_match_when_metrics_are_reused(self):
        """``build_overall_score_breakdown`` and ``build_habit_score_drivers``
        must produce identical output whether they recompute per-habit metrics
        themselves or reuse the ones already computed by ``compute_user_metrics``.

        The dashboard calls all three for the same window, so the latter path
        eliminates the duplicate habit iteration without changing the numbers.
        """
        start = self.today - timedelta(days=9)
        end = self.today
        habit_a = self._make_habit(name="Reuse A", start_date=start)
        ensure_initial_plan_version(habit_a)
        habit_b = self._make_habit(
            name="Reuse B",
            start_date=start,
            priority=Habit.PRIORITY_HIGH,
        )
        ensure_initial_plan_version(habit_b)
        for offset in range(10):
            HabitCompletion.objects.create(
                habit=habit_a,
                date=start + timedelta(days=offset),
                completion_percentage=Decimal("80"),
            )
            if offset % 2 == 0:
                HabitCompletion.objects.create(
                    habit=habit_b,
                    date=start + timedelta(days=offset),
                    completion_percentage=Decimal("100"),
                )

        habits = [habit_a, habit_b]

        # Path the dashboard actually takes.
        aggregated = compute_user_metrics(habits, start, end)
        breakdown_with_reuse = build_overall_score_breakdown(
            habits,
            start,
            end,
            start - timedelta(days=10),
            start - timedelta(days=1),
            precomputed_metrics=aggregated["per_habit"],
        )
        drivers_with_reuse = build_habit_score_drivers(
            habits,
            start,
            end,
            start - timedelta(days=10),
            start - timedelta(days=1),
            precomputed_metrics=aggregated["per_habit"],
        )

        # Path that recomputes everything from scratch (sanity check).
        breakdown_from_scratch = build_overall_score_breakdown(
            habits,
            start,
            end,
            start - timedelta(days=10),
            start - timedelta(days=1),
        )
        drivers_from_scratch = build_habit_score_drivers(
            habits,
            start,
            end,
            start - timedelta(days=10),
            start - timedelta(days=1),
        )

        self.assertEqual(
            breakdown_with_reuse["current_score"],
            breakdown_from_scratch["current_score"],
        )
        self.assertEqual(
            breakdown_with_reuse["scheduled_total"],
            breakdown_from_scratch["scheduled_total"],
        )
        self.assertEqual(
            breakdown_with_reuse["completed_total"],
            breakdown_from_scratch["completed_total"],
        )
        self.assertEqual(
            breakdown_with_reuse["score_delta"],
            breakdown_from_scratch["score_delta"],
        )
        for reused, fresh in zip(
            breakdown_with_reuse["components"],
            breakdown_from_scratch["components"],
        ):
            self.assertEqual(reused["key"], fresh["key"])
            self.assertEqual(reused["current_value"], fresh["current_value"])
            self.assertEqual(reused["current_points"], fresh["current_points"])
            self.assertEqual(reused["value_delta"], fresh["value_delta"])
            self.assertEqual(reused["points_delta"], fresh["points_delta"])

        for reused_driver, fresh_driver in zip(
            drivers_with_reuse.values(), drivers_from_scratch.values()
        ):
            self.assertEqual(
                (reused_driver["habit"].pk if reused_driver else None),
                (fresh_driver["habit"].pk if fresh_driver else None),
            )
            if reused_driver is None:
                self.assertIsNone(fresh_driver)
                continue
            self.assertEqual(
                reused_driver["score"], fresh_driver["score"]
            )
            self.assertEqual(
                reused_driver["impact_points"], fresh_driver["impact_points"]
            )
            self.assertEqual(
                reused_driver["drag_points"], fresh_driver["drag_points"]
            )
            self.assertEqual(
                reused_driver["score_delta"], fresh_driver["score_delta"]
            )


class PwaAssetDeliveryTests(TestCase):
    def test_service_worker_forces_updates_and_revalidates_unversioned_assets(self):
        response = self.client.get(reverse("service_worker"))

        self.assertEqual(response.status_code, 200)
        self.assertIn("no-cache", response["Cache-Control"])
        self.assertIn('const CACHE_NAME = "consistify-static-v6";', response.content.decode())
        self.assertIn("networkFirstStatic(request)", response.content.decode())

    def test_manifest_has_stable_identity_and_requires_revalidation(self):
        response = self.client.get(reverse("manifest"))

        self.assertEqual(response.status_code, 200)
        self.assertIn("no-cache", response["Cache-Control"])
        self.assertEqual(response.json()["id"], "/")


class DashboardPresentationTests(TestCase):
    def test_dashboard_contains_responsive_stat_labels_and_weighted_rate_note(self):
        user = get_user_model().objects.create_user(
            username="dashboard-presentation-user",
            password="not-used",
        )
        self.client.force_login(user)

        response = self.client.get(reverse("habits:dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Performance dashboard")
        self.assertContains(response, 'class="dashboard-title-mobile">Dashboard</span>')
        self.assertContains(response, "Overall completion")
        self.assertContains(response, "Completion")
        self.assertContains(response, "Scheduled sessions")
        self.assertContains(response, "Scheduled")
        self.assertContains(response, "Completed sessions")
        self.assertContains(response, "Completed")
        self.assertContains(response, "Consistency score")
        self.assertContains(response, "Consistency")
        self.assertContains(response, "Priority-weighted")
        self.assertEqual(
            response.content.decode().count('class="score-driver-help"'),
            12,
        )
        self.assertContains(response, "Average progress across scheduled sessions")
        self.assertContains(response, "Total habit sessions scheduled")
        self.assertContains(response, "reached 100% completion")
        self.assertContains(
            response,
            "Consistify Score = 35% Completion Quality + 20% Full Completion + 30% Consistency Rhythm + 15% Recent Momentum",
        )
        self.assertContains(
            response,
            "The percentage of scheduled sessions you finished at 100%",
        )
        self.assertContains(response, "helping your overall Consistency Score")
        self.assertContains(response, "holding your overall Consistency Score back")
        self.assertContains(response, "largest increase in Consistency Score")
        self.assertContains(response, "largest decrease in Consistency Score")


class HabitFormPresentationTests(TestCase):
    def test_target_value_display_strips_unnecessary_trailing_zeros(self):
        user = get_user_model().objects.create_user(
            username="habit-form-display-user",
            password="not-used",
        )
        integer_habit = Habit.objects.create(
            user=user,
            name="Integer target",
            habit_type=Habit.HABIT_QUANTITATIVE,
            schedule_type=Habit.SCHEDULE_DAILY,
            start_date=date(2026, 1, 1),
            target_value=Decimal("10"),
            unit="pages",
        )
        decimal_habit = Habit.objects.create(
            user=user,
            name="Decimal target",
            habit_type=Habit.HABIT_QUANTITATIVE,
            schedule_type=Habit.SCHEDULE_DAILY,
            start_date=date(2026, 1, 1),
            target_value=Decimal("10.5"),
            unit="pages",
        )

        integer_target_html = str(HabitForm(instance=integer_habit)["target_value"])
        decimal_target_html = str(HabitForm(instance=decimal_habit)["target_value"])

        self.assertIn('value="10"', integer_target_html)
        self.assertNotIn('value="10.0"', integer_target_html)
        self.assertIn('value="10.5"', decimal_target_html)
        self.assertNotIn('value="10.50"', decimal_target_html)

    def test_driver_cards_keep_current_metrics_in_desktop_markup(self):
        user = get_user_model().objects.create_user(
            username="dashboard-driver-user",
            password="not-used",
        )
        habit = Habit.objects.create(
            user=user,
            name="Driver habit",
            schedule_type=Habit.SCHEDULE_DAILY,
            start_date=timezone.localdate(),
        )
        base_driver = {
            "habit": habit,
            "score": 91.0,
            "completion_rate": 88.0,
            "impact_points": 12.3,
            "drag_points": 4.5,
            "score_delta": 6.7,
        }
        declined_driver = {**base_driver, "score_delta": -6.7}
        self.client.force_login(user)

        with patch(
            "habits.views.build_habit_score_drivers",
            return_value={
                "booster": base_driver,
                "drag": base_driver,
                "improved": base_driver,
                "declined": declined_driver,
            },
        ):
            response = self.client.get(reverse("habits:dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Driver habit")
        self.assertContains(
            response,
            "91.0 consistency &middot; 88.0% completion",
        )


class ConsistencyScoreTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="score-user",
            password="not-used",
        )
        self.categories = {}
        for index, (key, label) in enumerate(DEFAULT_CATEGORIES, start=1):
            category, _ = HabitCategory.objects.get_or_create(
                key=key,
                defaults={"label": label, "sort_order": index},
            )
            self.categories[key] = category

    def _create_habit(
        self,
        name,
        start_date,
        habit_type=Habit.HABIT_PARTIAL,
        schedule_type=Habit.SCHEDULE_DAILY,
        priority=Habit.PRIORITY_MEDIUM,
    ):
        return Habit.objects.create(
            user=self.user,
            name=name,
            habit_type=habit_type,
            schedule_type=schedule_type,
            priority=priority,
            start_date=start_date,
        )

    def make_habit(
        self,
        name,
        start_date,
        habit_type=Habit.HABIT_PARTIAL,
        schedule_type=Habit.SCHEDULE_DAILY,
        priority=Habit.PRIORITY_MEDIUM,
        categories=None,
    ):
        habit = self._create_habit(
            name,
            start_date,
            habit_type=habit_type,
            schedule_type=schedule_type,
            priority=priority,
        )
        if categories is None:
            categories = [self.categories["spiritual"]]
        habit.categories.set(categories)
        return habit

    def log_completion(self, habit, target_date, percentage):
        value = Decimal(str(percentage))
        return HabitCompletion.objects.create(
            habit=habit,
            date=target_date,
            completion_percentage=value,
            raw_value=value,
        )

    def test_partial_progress_contributes_to_consistency_score(self):
        start = date(2026, 1, 1)
        habit = self.make_habit("Read", start)
        for offset in range(4):
            self.log_completion(habit, start + timedelta(days=offset), 90)

        metrics = habit_performance_metrics(habit, start, start + timedelta(days=3))

        self.assertEqual(metrics["scheduled_total"], 4)
        self.assertEqual(metrics["completed_total"], 0)
        self.assertEqual(metrics["completion_rate"], 90.0)
        self.assertEqual(metrics["completion_quality"], 90.0)
        self.assertEqual(metrics["full_completion_reliability"], 0.0)
        self.assertEqual(metrics["streak_stability"], 100.0)
        self.assertEqual(metrics["recent_momentum"], 50.0)
        # Consistify Score = 0.35*90 + 0.20*0 + 0.30*100 + 0.15*50 = 69.0
        self.assertEqual(metrics["consistency_score"], 69.0)

    def test_quality_is_proportional_and_full_completion_remains_exact(self):
        start = date(2026, 1, 1)
        habit = self.make_habit("Quality boundaries", start)
        for offset, percentage in enumerate([100, 99, 50, 0]):
            self.log_completion(habit, start + timedelta(days=offset), percentage)

        metrics = habit_performance_metrics(
            habit,
            start,
            start + timedelta(days=4),
        )

        self.assertEqual(metrics["scheduled_total"], 5)
        self.assertEqual(metrics["completion_quality"], 49.8)
        self.assertEqual(metrics["full_completion_reliability"], 20.0)
        self.assertEqual(metrics["completed_total"], 1)

    def test_consistency_rhythm_decreases_as_consecutive_misses_accumulate(self):
        start = date(2026, 1, 1)
        habit = self.make_habit("Fading rhythm", start)
        self.log_completion(habit, start, 100)
        self.log_completion(habit, start + timedelta(days=1), 51)

        stable_metrics = habit_performance_metrics(
            habit,
            start,
            start + timedelta(days=1),
        )
        one_miss_metrics = habit_performance_metrics(
            habit,
            start,
            start + timedelta(days=2),
        )
        two_miss_metrics = habit_performance_metrics(
            habit,
            start,
            start + timedelta(days=3),
        )

        self.assertEqual(stable_metrics["streak_stability"], 83.3)
        self.assertEqual(one_miss_metrics["streak_stability"], 63.3)
        self.assertEqual(two_miss_metrics["streak_stability"], 46.7)
        self.assertGreater(
            stable_metrics["streak_stability"],
            one_miss_metrics["streak_stability"],
        )
        self.assertGreater(
            one_miss_metrics["streak_stability"],
            two_miss_metrics["streak_stability"],
        )

    def test_consistency_rhythm_does_not_recover_without_new_effort(self):
        start = date(2026, 1, 1)
        habit = self.make_habit("Broken rhythm", start)
        self.log_completion(habit, start, 100)

        one_miss_metrics = habit_performance_metrics(
            habit,
            start,
            start + timedelta(days=1),
        )
        six_miss_metrics = habit_performance_metrics(
            habit,
            start,
            start + timedelta(days=6),
        )
        seven_miss_metrics = habit_performance_metrics(
            habit,
            start,
            start + timedelta(days=7),
        )

        self.assertEqual(one_miss_metrics["streak_stability"], 43.3)
        self.assertEqual(six_miss_metrics["streak_stability"], 11.4)
        self.assertEqual(seven_miss_metrics["streak_stability"], 0.0)
        self.assertGreater(
            one_miss_metrics["streak_stability"],
            six_miss_metrics["streak_stability"],
        )
        self.assertGreater(
            six_miss_metrics["streak_stability"],
            seven_miss_metrics["streak_stability"],
        )

    def test_consistency_rhythm_requires_more_than_fifty_percent(self):
        start = date(2026, 1, 1)
        habit = self.make_habit("Threshold rhythm", start)
        self.log_completion(habit, start, 100)
        self.log_completion(habit, start + timedelta(days=1), 50)
        self.log_completion(habit, start + timedelta(days=2), Decimal("50.01"))

        metrics_at_threshold = habit_performance_metrics(
            habit,
            start,
            start + timedelta(days=1),
        )
        metrics_above_threshold = habit_performance_metrics(
            habit,
            start,
            start + timedelta(days=2),
        )

        self.assertEqual(metrics_at_threshold["streak_stability"], 43.3)
        self.assertEqual(metrics_above_threshold["streak_stability"], 53.3)

    def test_consistency_rhythm_uses_only_the_seven_most_recent_sessions(self):
        start = date(2026, 1, 1)
        habit = self.make_habit("Recent rhythm", start)
        self.log_completion(habit, start, 0)
        self.log_completion(habit, start + timedelta(days=1), 0)
        for offset in range(2, 8):
            self.log_completion(habit, start + timedelta(days=offset), 80)

        metrics = habit_performance_metrics(
            habit,
            start,
            start + timedelta(days=7),
        )

        self.assertEqual(metrics["streak_stability"], 85.2)

    def test_consistency_rhythm_confidence_requires_three_sessions(self):
        start = date(2026, 1, 1)
        habit = self.make_habit("Building confidence", start)
        for offset in range(3):
            self.log_completion(habit, start + timedelta(days=offset), 80)

        one_session = habit_performance_metrics(habit, start, start)
        two_sessions = habit_performance_metrics(
            habit,
            start,
            start + timedelta(days=1),
        )
        three_sessions = habit_performance_metrics(
            habit,
            start,
            start + timedelta(days=2),
        )

        self.assertEqual(one_session["streak_stability"], 66.7)
        self.assertEqual(two_sessions["streak_stability"], 83.3)
        self.assertEqual(three_sessions["streak_stability"], 100.0)

    def test_rhythm_rewards_continuity_when_success_coverage_is_equal(self):
        start = date(2026, 1, 1)
        alternating = self.make_habit("Alternating rhythm", start)
        grouped = self.make_habit("Grouped rhythm", start)
        for offset, percentage in enumerate([80, 20, 80, 20, 80]):
            self.log_completion(
                alternating,
                start + timedelta(days=offset),
                percentage,
            )
        for offset, percentage in enumerate([20, 20, 80, 80, 80]):
            self.log_completion(
                grouped,
                start + timedelta(days=offset),
                percentage,
            )

        alternating_metrics = habit_performance_metrics(
            alternating,
            start,
            start + timedelta(days=4),
        )
        grouped_metrics = habit_performance_metrics(
            grouped,
            start,
            start + timedelta(days=4),
        )

        self.assertEqual(alternating_metrics["streak_stability"], 48.0)
        self.assertEqual(grouped_metrics["streak_stability"], 58.0)

    def test_rhythm_and_momentum_measure_level_and_improvement_separately(self):
        start = date(2026, 1, 1)
        recovering = self.make_habit("Recovering", start)
        steady = self.make_habit("Steady", start)
        self.log_completion(recovering, start, 30)
        self.log_completion(recovering, start + timedelta(days=1), 80)
        self.log_completion(steady, start, 80)
        self.log_completion(steady, start + timedelta(days=1), 75)

        recovering_metrics = habit_performance_metrics(
            recovering,
            start,
            start + timedelta(days=1),
        )
        steady_metrics = habit_performance_metrics(
            steady,
            start,
            start + timedelta(days=1),
        )

        self.assertEqual(recovering_metrics["streak_stability"], 43.3)
        self.assertEqual(recovering_metrics["recent_momentum"], 83.3)
        self.assertEqual(steady_metrics["streak_stability"], 83.3)
        self.assertEqual(steady_metrics["recent_momentum"], 50.0)

    def test_momentum_ignores_noise_without_meaningful_progress(self):
        start = date(2026, 1, 1)
        habit = self.make_habit("Noise only", start)
        self.log_completion(habit, start, 0)
        self.log_completion(habit, start + timedelta(days=1), Decimal("0.01"))

        metrics = habit_performance_metrics(
            habit,
            start,
            start + timedelta(days=1),
        )

        self.assertEqual(metrics["recent_momentum"], 0.0)

    def test_momentum_ignores_changes_within_five_percentage_points(self):
        start = date(2026, 1, 1)
        expected_momentum = {
            75: 50.0,
            85: 50.0,
            74: 49.3,
            86: 50.7,
        }

        for current_value, expected in expected_momentum.items():
            habit = self.make_habit(f"Noise boundary {current_value}", start)
            self.log_completion(habit, start, 80)
            self.log_completion(habit, start + timedelta(days=1), current_value)

            with self.subTest(current_value=current_value):
                metrics = habit_performance_metrics(
                    habit,
                    start,
                    start + timedelta(days=1),
                )
                self.assertEqual(metrics["recent_momentum"], expected)

    def test_stable_low_progress_cannot_receive_high_momentum(self):
        start = date(2026, 1, 1)

        for completion, expected in ((5, 5.0), (10, 10.0)):
            habit = self.make_habit(f"Stable at {completion}", start)
            for offset in range(6):
                self.log_completion(
                    habit,
                    start + timedelta(days=offset),
                    completion,
                )

            with self.subTest(completion=completion):
                metrics = habit_performance_metrics(
                    habit,
                    start,
                    start + timedelta(days=5),
                )
                self.assertEqual(metrics["recent_momentum"], expected)

    def test_old_high_progress_cannot_prop_up_sustained_low_momentum(self):
        start = date(2026, 1, 1)
        habit = self.make_habit("Old spike", start)
        for offset, completion in enumerate([100, 10, 10, 10, 10, 10]):
            self.log_completion(
                habit,
                start + timedelta(days=offset),
                completion,
            )

        metrics = habit_performance_metrics(
            habit,
            start,
            start + timedelta(days=5),
        )

        self.assertEqual(metrics["recent_momentum"], 9.3)

    def test_momentum_is_equal_for_the_same_daily_and_weekly_recovery(self):
        start = date(2026, 1, 1)
        daily = self.make_habit("Daily recovery", start)
        weekly = self.make_habit(
            "Weekly recovery",
            start,
            schedule_type=Habit.SCHEDULE_WEEKLY,
        )
        self.log_completion(daily, start, 30)
        self.log_completion(daily, start + timedelta(days=1), 80)
        self.log_completion(weekly, start, 30)
        self.log_completion(weekly, start + timedelta(days=7), 80)

        daily_metrics = habit_performance_metrics(
            daily,
            start,
            start + timedelta(days=1),
        )
        weekly_metrics = habit_performance_metrics(
            weekly,
            start,
            start + timedelta(days=7),
        )

        self.assertEqual(daily_metrics["recent_momentum"], 83.3)
        self.assertEqual(weekly_metrics["recent_momentum"], 83.3)
        self.assertEqual(daily_metrics["streak_stability"], 43.3)
        self.assertEqual(weekly_metrics["streak_stability"], 43.3)
        # Q=55, F=0, R=43.33..., M=83.33...  =>  score 44.75 rounded to 44.8.
        self.assertEqual(daily_metrics["consistency_score"], 44.8)
        self.assertEqual(weekly_metrics["consistency_score"], 44.8)

    def test_momentum_uses_only_the_six_most_recent_scheduled_sessions(self):
        start = date(2026, 1, 1)
        habit = self.make_habit("Settled recovery", start)
        for offset, percentage in enumerate([0, 100, 100, 100, 100, 100, 100]):
            self.log_completion(habit, start + timedelta(days=offset), percentage)

        metrics = habit_performance_metrics(
            habit,
            start,
            start + timedelta(days=6),
        )

        self.assertEqual(metrics["recent_momentum"], 50.0)

    def test_recent_momentum_rewards_improvement(self):
        start = date(2026, 1, 1)
        improving = self.make_habit("Improving", start)
        declining = self.make_habit("Declining", start)

        for offset, percentage in enumerate([0, 50, 100, 100]):
            self.log_completion(improving, start + timedelta(days=offset), percentage)
        for offset, percentage in enumerate([100, 100, 50, 0]):
            self.log_completion(declining, start + timedelta(days=offset), percentage)

        improving_metrics = habit_performance_metrics(
            improving,
            start,
            start + timedelta(days=3),
        )
        declining_metrics = habit_performance_metrics(
            declining,
            start,
            start + timedelta(days=3),
        )

        self.assertEqual(improving_metrics["completion_rate"], 62.5)
        self.assertEqual(declining_metrics["completion_rate"], 62.5)
        self.assertGreater(
            improving_metrics["recent_momentum"],
            declining_metrics["recent_momentum"],
        )
        self.assertGreater(
            improving_metrics["consistency_score"],
            declining_metrics["consistency_score"],
        )

    def test_recent_momentum_treats_stable_daily_and_weekly_habits_equally(self):
        start = date(2026, 1, 1)
        end = start + timedelta(days=13)
        daily = self.make_habit("Perfect daily", start)
        weekly = self.make_habit(
            "Perfect weekly",
            start,
            schedule_type=Habit.SCHEDULE_WEEKLY,
        )

        for offset in range(14):
            self.log_completion(daily, start + timedelta(days=offset), 100)
        self.log_completion(weekly, start, 100)
        self.log_completion(weekly, start + timedelta(days=7), 100)

        daily_metrics = habit_performance_metrics(daily, start, end)
        weekly_metrics = habit_performance_metrics(weekly, start, end)

        self.assertEqual(daily_metrics["recent_momentum"], 50.0)
        self.assertEqual(weekly_metrics["recent_momentum"], 50.0)
        # Daily: 0.35*100 + 0.20*100 + 0.30*100 + 0.15*50 = 92.5
        self.assertEqual(daily_metrics["consistency_score"], 92.5)
        # Weekly: 0.35*100 + 0.20*100 + 0.30*83.3 + 0.15*50 = 87.5
        self.assertEqual(weekly_metrics["consistency_score"], 87.5)

    def test_rhythm_high_and_momentum_low_are_independent(self):
        """High rhythm (flat reliability) must not be inflated by momentum.

        A long, flat-perfect streak keeps ``recent_momentum`` at the neutral
        50 because there is no upward or downward trajectory. ``consistency_
        rhythm`` should report a high value because every session crosses
        the threshold and every transition is successful.
        """
        start = date(2026, 1, 1)
        habit = self.make_habit("Flat perfect streak", start)
        for offset in range(7):
            self.log_completion(habit, start + timedelta(days=offset), 100)

        metrics = habit_performance_metrics(
            habit, start, start + timedelta(days=6),
        )

        # Rhythm covers the whole 7-session window with full reliability.
        self.assertEqual(metrics["streak_stability"], 100.0)
        # No transitions means no trajectory — momentum is neutral at 50.
        self.assertEqual(metrics["recent_momentum"], 50.0)
        # R is high, M is neutral: the score is anchored by Q, R, and F
        # without an artificial boost from momentum.
        # 0.35*100 + 0.20*100 + 0.30*100 + 0.15*50 = 92.5
        self.assertEqual(metrics["consistency_score"], 92.5)

    def test_momentum_high_and_rhythm_low_are_independent(self):
        """High momentum (sharp recent improvement) must not inflate rhythm.

        A sudden lift at the tail end of an otherwise low session history
        gives a strong trajectory signal but the rhythm (coverage/continuity
        over the last 7 sessions) is dragged down by the missed earlier
        sessions. The two signals must not bleed into each other.
        """
        start = date(2026, 1, 1)
        habit = self.make_habit("Late sprint", start)
        # Five sessions well below threshold, then a sharp upward spike.
        for offset in range(5):
            self.log_completion(habit, start + timedelta(days=offset), 20)
        self.log_completion(habit, start + timedelta(days=5), 100)

        metrics = habit_performance_metrics(
            habit, start, start + timedelta(days=5),
        )

        # Rhythm is low: 1/6 coverage with full confidence; raw coverage
        # is 1/6, so even with continuity=0 the raw is small and gets shrunk
        # toward 50. The reported streak_stability must stay modest.
        self.assertLess(metrics["streak_stability"], 30.0)
        # Momentum is positive because the only meaningful change is a +80
        # lift, which yields a strong signal. The damped result remains
        # clearly above the deadband neutral.
        self.assertGreater(metrics["recent_momentum"], 50.0)
        # The score is dominated by low Q and low R; even with a positive
        # M the overall score stays modest. If rhythm and momentum are
        # independent (the desired property), no special case inflates the
        # result above what the components justify.
        self.assertLess(metrics["consistency_score"], 55.0)

    def test_recent_momentum_excludes_paused_dates_from_its_denominator(self):
        start = date(2026, 1, 1)
        end = start + timedelta(days=13)
        habit = self.make_habit("Paused then perfect", start)
        HabitPause.objects.create(
            habit=habit,
            start_date=start,
            end_date=start + timedelta(days=7),
        )
        for offset in range(7, 14):
            self.log_completion(habit, start + timedelta(days=offset), 100)

        metrics = habit_performance_metrics(habit, start, end)

        self.assertEqual(metrics["scheduled_total"], 7)
        self.assertEqual(metrics["recent_momentum"], 50.0)
        self.assertEqual(metrics["consistency_score"], 92.5)

    def test_recent_momentum_does_not_decay_during_a_long_pause(self):
        start = date(2026, 1, 1)
        end = start + timedelta(days=29)
        habit = self.make_habit("Long pause", start)
        for offset in range(16):
            self.log_completion(habit, start + timedelta(days=offset), 100)
        HabitPause.objects.create(
            habit=habit,
            start_date=start + timedelta(days=16),
            end_date=None,
        )

        metrics = habit_performance_metrics(habit, start, end)

        self.assertEqual(metrics["scheduled_total"], 16)
        self.assertEqual(metrics["recent_momentum"], 50.0)
        self.assertEqual(metrics["consistency_score"], 92.5)

    def test_recent_components_are_isolated_to_the_report_period(self):
        start = date(2026, 1, 1)
        report_start = start + timedelta(days=7)
        report_end = start + timedelta(days=8)
        strong_history = self.make_habit("Strong prior history", start)
        weak_history = self.make_habit("Weak prior history", start)
        for offset in range(7):
            self.log_completion(
                strong_history,
                start + timedelta(days=offset),
                100,
            )
            self.log_completion(
                weak_history,
                start + timedelta(days=offset),
                0,
            )
        for habit in (strong_history, weak_history):
            self.log_completion(habit, report_start, 30)
            self.log_completion(habit, report_end, 80)

        strong_metrics = habit_performance_metrics(
            strong_history,
            report_start,
            report_end,
        )
        weak_metrics = habit_performance_metrics(
            weak_history,
            report_start,
            report_end,
        )
        cached_metrics = habit_performance_metrics(
            strong_history,
            report_start,
            report_end,
            completion_map={
                report_start: 30,
                report_end: 80,
            },
            value_map={},
        )

        expected = {
            "scheduled_total": 2,
            "completion_quality": 55.0,
            "full_completion_reliability": 0.0,
            "streak_stability": 43.3,
            "recent_momentum": 83.3,
            # Q=55, F=0, R=43.33..., M=83.33...  =>  score=44.75, rounded 44.8.
            "consistency_score": 44.8,
        }
        for key, value in expected.items():
            self.assertEqual(strong_metrics[key], value)
            self.assertEqual(weak_metrics[key], value)
            self.assertEqual(cached_metrics[key], value)

    def test_plan_edits_preserve_historical_schedule_score_and_categories(self):
        start = date(2026, 1, 1)
        today = start + timedelta(days=6)
        habit = self.make_habit(
            "Versioned routine",
            start,
            categories=[self.categories["spiritual"]],
        )
        self.log_completion(habit, start, 100)
        self.log_completion(habit, start + timedelta(days=1), 80)
        ensure_initial_plan_version(habit)

        before_metrics = habit_performance_metrics(habit, start, today)
        before_dates = list(iter_scheduled_dates(habit, start, today))

        schedule_habit_plan_edit(
            habit,
            schedule_type=Habit.SCHEDULE_WEEKLY,
            start_date=today + timedelta(days=1),
            weekly_interval=1,
            priority=Habit.PRIORITY_HIGH,
            categories=[self.categories["health"]],
            today=today,
        )

        after_metrics = habit_performance_metrics(habit, start, today)
        after_dates = list(iter_scheduled_dates(habit, start, today))
        historical_categories = {
            item["key"]: item
            for item in build_category_analytics([habit], start, today)["summaries"]
        }
        future_categories = {
            item["key"]: item
            for item in build_category_analytics(
                [habit],
                today + timedelta(days=1),
                today + timedelta(days=8),
            )["summaries"]
        }

        self.assertEqual(after_metrics, before_metrics)
        self.assertEqual(after_dates, before_dates)
        self.assertEqual(len(after_dates), 7)
        self.assertEqual(
            list(
                iter_scheduled_dates(
                    habit,
                    today + timedelta(days=1),
                    today + timedelta(days=15),
                )
            ),
            [
                today + timedelta(days=1),
                today + timedelta(days=8),
                today + timedelta(days=15),
            ],
        )
        self.assertEqual(historical_categories["spiritual"]["scheduled_total"], 7)
        self.assertEqual(historical_categories["health"]["scheduled_total"], 0)
        self.assertEqual(future_categories["spiritual"]["scheduled_total"], 0)
        self.assertEqual(future_categories["health"]["scheduled_total"], 2)

    def test_priority_edit_changes_future_but_not_historical_aggregation(self):
        start = date(2026, 1, 1)
        today = start + timedelta(days=2)
        completed = self.make_habit("Completed", start)
        missed = self.make_habit("Missed", start)
        for offset in range(3):
            self.log_completion(completed, start + timedelta(days=offset), 100)
        ensure_initial_plan_version(completed)
        ensure_initial_plan_version(missed)

        before = compute_user_metrics([completed, missed], start, today)
        schedule_habit_plan_edit(
            completed,
            priority=Habit.PRIORITY_HIGH,
            today=today,
        )
        after = compute_user_metrics([completed, missed], start, today)

        tomorrow = today + timedelta(days=1)
        self.log_completion(completed, tomorrow, 100)
        future = compute_user_metrics([completed, missed], tomorrow, tomorrow)

        self.assertEqual(after["aggregate"], before["aggregate"])
        self.assertEqual(after["consistency_score"], before["consistency_score"])
        self.assertEqual(future["aggregate"]["completion_rate"], 56.5)

    def test_completion_rate_uses_priority_active_on_each_occurrence(self):
        start = date(2026, 1, 1)
        habit = self.make_habit("Changing priority", start)
        self.log_completion(habit, start, 100)
        ensure_initial_plan_version(habit)
        schedule_habit_plan_edit(
            habit,
            priority=Habit.PRIORITY_HIGH,
            today=start,
        )

        metrics = habit_performance_metrics(
            habit,
            start,
            start + timedelta(days=1),
        )

        self.assertEqual(metrics["completion_quality"], 50.0)
        self.assertEqual(metrics["completion_rate"], 43.5)

    def test_repeated_same_day_plan_edits_share_one_pending_version(self):
        today = date(2026, 1, 3)
        habit = self.make_habit("Pending plan", date(2026, 1, 1))
        ensure_initial_plan_version(habit)

        schedule_habit_plan_edit(
            habit,
            priority=Habit.PRIORITY_HIGH,
            today=today,
        )
        schedule_habit_plan_edit(
            habit,
            schedule_type=Habit.SCHEDULE_WEEKLY,
            start_date=today + timedelta(days=1),
            priority=Habit.PRIORITY_LOW,
            today=today,
        )

        versions = list(
            HabitPlanVersion.objects.filter(habit=habit).order_by("effective_from")
        )
        self.assertEqual(len(versions), 2)
        self.assertEqual(versions[-1].effective_from, today + timedelta(days=1))
        self.assertEqual(versions[-1].schedule_type, Habit.SCHEDULE_WEEKLY)
        self.assertEqual(versions[-1].priority, Habit.PRIORITY_LOW)

    def test_tracking_start_ignores_a_superseded_future_anchor(self):
        habit = self.make_habit("Deferred twice", date(2026, 1, 10))
        first = HabitPlanVersion.objects.create(
            habit=habit,
            effective_from=date(2026, 1, 1),
            schedule_anchor=date(2026, 1, 10),
            schedule_type=Habit.SCHEDULE_DAILY,
            priority=Habit.PRIORITY_MEDIUM,
        )
        first.categories.set([self.categories["spiritual"]])
        second = HabitPlanVersion.objects.create(
            habit=habit,
            effective_from=date(2026, 1, 6),
            schedule_anchor=date(2026, 1, 20),
            schedule_type=Habit.SCHEDULE_DAILY,
            priority=Habit.PRIORITY_MEDIUM,
        )
        second.categories.set([self.categories["spiritual"]])

        self.assertEqual(habit_tracking_start(habit), date(2026, 1, 20))

    def test_habit_edit_view_versions_plan_changes_for_tomorrow(self):
        start = date(2026, 1, 1)
        today = start + timedelta(days=6)
        habit = self.make_habit(
            "Edit through view",
            start,
            categories=[self.categories["spiritual"]],
        )
        self.log_completion(habit, start, 100)
        before = habit_performance_metrics(habit, start, today)
        self.client.force_login(self.user)

        with patch("habits.views.timezone.localdate", return_value=today):
            response = self.client.post(
                reverse("habits:habit_edit", args=[habit.id]),
                {
                    "name": habit.name,
                    "description": "",
                    "habit_type": Habit.HABIT_PARTIAL,
                    "target_value": "",
                    "unit": "",
                    "schedule_type": Habit.SCHEDULE_WEEKLY,
                    "categories": [self.categories["health"].id],
                    "priority": Habit.PRIORITY_HIGH,
                    "tags": "",
                    "start_date": (today + timedelta(days=1)).isoformat(),
                    "interval_days": 1,
                    "weekly_interval": 1,
                    "days_of_week": [],
                },
            )

        self.assertRedirects(
            response,
            reverse("habits:habit_detail", args=[habit.id]),
            fetch_redirect_response=False,
        )
        habit.refresh_from_db()
        self.assertEqual(habit.schedule_type, Habit.SCHEDULE_WEEKLY)
        self.assertEqual(habit.priority, Habit.PRIORITY_HIGH)
        self.assertEqual(
            habit_performance_metrics(habit, start, today),
            before,
        )
        pending = HabitPlanVersion.objects.get(
            habit=habit,
            effective_from=today + timedelta(days=1),
        )
        self.assertEqual(pending.schedule_type, Habit.SCHEDULE_WEEKLY)
        self.assertEqual(
            list(pending.categories.values_list("pk", flat=True)),
            [self.categories["health"].id],
        )

        with patch("habits.views.timezone.localdate", return_value=today):
            detail = self.client.get(
                reverse("habits:habit_detail", args=[habit.id])
            )
        displayed_habit = detail.context["habit"]
        self.assertEqual(displayed_habit.active_schedule_summary, "Every day")
        self.assertEqual(
            displayed_habit.active_priority,
            Habit.PRIORITY_MEDIUM,
        )
        self.assertEqual(
            [category.id for category in displayed_habit.active_categories],
            [self.categories["spiritual"].id],
        )
        self.assertEqual(
            displayed_habit.pending_plan_date,
            today + timedelta(days=1),
        )

    def test_historical_completion_edit_recomputes_score_without_stale_cache(self):
        start = date(2026, 1, 1)
        habit = self.make_habit("Editable outcome", start)
        completion = self.log_completion(habit, start, 80)
        ensure_initial_plan_version(habit)

        before = habit_performance_metrics(habit, start, start)
        completion.completion_percentage = Decimal("100")
        completion.raw_value = Decimal("100")
        completion.save(update_fields=["completion_percentage", "raw_value"])
        after = habit_performance_metrics(habit, start, start)

        self.assertEqual(before["completion_quality"], 80.0)
        self.assertEqual(before["full_completion_reliability"], 0.0)
        self.assertEqual(after["completion_quality"], 100.0)
        self.assertEqual(after["full_completion_reliability"], 100.0)
        self.assertGreater(after["consistency_score"], before["consistency_score"])

    def test_daily_completion_series_counts_unlogged_today_as_zero(self):
        today = date(2026, 1, 2)
        first = self.make_habit("Series complete", today)
        second = self.make_habit("Series pending", today)
        self.log_completion(first, today, 100)

        series = daily_average_completion_series(
            [first, second],
            today - timedelta(days=1),
            today,
        )

        self.assertIsNone(series[0]["value"])
        self.assertEqual(series[1]["value"], 50.0)

    def test_historical_rate_weights_every_scheduled_session_by_priority(self):
        start = date(2026, 1, 5)
        end = start + timedelta(days=3)
        high_weekly = self.make_habit(
            "High weekly partial",
            start,
            schedule_type=Habit.SCHEDULE_WEEKLY,
            priority=Habit.PRIORITY_HIGH,
        )
        low_daily = self.make_habit(
            "Low daily complete",
            start,
            priority=Habit.PRIORITY_LOW,
        )
        self.log_completion(high_weekly, start, 50)
        for offset in range(4):
            self.log_completion(low_daily, start + timedelta(days=offset), 100)

        # (1.3 * 50 + 0.8 * 400) / (1.3 * 1 + 0.8 * 4)
        expected_rate = 85.6
        aggregate = compute_user_metrics([high_weekly, low_daily], start, end)
        weekly_report = build_weekly_reports(
            [high_weekly, low_daily],
            weeks=1,
            today=end,
        )[0]
        category_summaries = {
            item["key"]: item
            for item in build_category_analytics(
                [high_weekly, low_daily],
                start,
                end,
            )["summaries"]
        }

        self.assertEqual(aggregate["aggregate"]["completion_rate"], expected_rate)
        self.assertEqual(weekly_report["completion_rate"], expected_rate)
        self.assertEqual(
            category_summaries["spiritual"]["completion_rate"],
            expected_rate,
        )

    def test_overall_consistency_uses_priority_and_sqrt_frequency_weighting(self):
        start = date(2026, 1, 1)
        high_priority = self.make_habit(
            "Important weekly habit",
            start,
            schedule_type=Habit.SCHEDULE_WEEKLY,
            priority=Habit.PRIORITY_HIGH,
        )
        low_priority = self.make_habit(
            "Low priority daily habit",
            start,
            priority=Habit.PRIORITY_LOW,
        )
        self.log_completion(high_priority, start, 100)

        overall_score = calculate_overall_consistency(
            [high_priority, low_priority],
            start,
            start + timedelta(days=3),
        )

        # Stable performance receives neutral momentum regardless of cadence.
        # The daily habit has no logged completions, so its score is 0.
        # The weekly habit has Q=100, F=100, R=66.7 (1-session shrunk),
        # M=50  =>  0.35*100 + 0.20*100 + 0.30*66.7 + 0.15*50 = 82.5.
        high_weight = 1.3 * sqrt(1)
        low_weight = 0.8 * sqrt(4)
        weekly_score = 82.5
        expected_score = round(
            (weekly_score * high_weight + 0 * low_weight)
            / (high_weight + low_weight),
            1,
        )
        self.assertEqual(overall_score, expected_score)

    def test_unlogged_today_matches_an_explicit_zero_completion(self):
        today = date(2026, 1, 2)
        yesterday = today - timedelta(days=1)
        habit = self.make_habit("Daily", yesterday)
        self.log_completion(habit, yesterday, 100)

        with patch("habits.services.timezone.localdate", return_value=today):
            metrics = habit_performance_metrics(habit, yesterday, today)

        self.assertEqual(metrics["scheduled_total"], 2)
        self.assertEqual(metrics["completed_total"], 1)
        self.assertEqual(metrics["completion_rate"], 50.0)
        self.assertEqual(metrics["current_streak"], 0)
        self.assertEqual(metrics["recent_momentum"], 0.0)
        # Q=50, F=50, R=43.3, M=0  =>  0.35*50 + 0.20*50 + 0.30*43.3 + 0.15*0 = 40.5
        self.assertEqual(metrics["consistency_score"], 40.5)
        unlogged_metrics = metrics

        self.log_completion(habit, today, 0)
        with patch("habits.services.timezone.localdate", return_value=today):
            metrics = habit_performance_metrics(habit, yesterday, today)

        self.assertEqual(metrics["scheduled_total"], 2)
        self.assertEqual(metrics["completed_total"], 1)
        self.assertEqual(metrics["current_streak"], 0)
        self.assertEqual(metrics, unlogged_metrics)

    def test_overall_score_breakdown_explains_score_change(self):
        start = date(2026, 1, 1)
        habit = self.make_habit("Better routine", start)
        for offset in range(4):
            self.log_completion(habit, start + timedelta(days=offset), 50)
            self.log_completion(habit, start + timedelta(days=offset + 4), 100)

        breakdown = build_overall_score_breakdown(
            [habit],
            start + timedelta(days=4),
            start + timedelta(days=7),
            start,
            start + timedelta(days=3),
        )

        components = {item["key"]: item for item in breakdown["components"]}

        self.assertTrue(breakdown["has_previous"])
        # current = 0.35*100 + 0.20*100 + 0.30*100 + 0.15*50 = 92.5
        self.assertEqual(breakdown["current_score"], 92.5)
        # previous = 0.35*50 + 0.20*0 + 0.30*0 + 0.15*50 = 25.0
        self.assertEqual(breakdown["previous_score"], 25.0)
        self.assertEqual(breakdown["score_delta"], 67.5)
        # Q delta = (100-50) * 0.35 = 17.5
        self.assertEqual(components["completion_quality"]["points_delta"], 17.5)
        # F delta = (100-0) * 0.20 = 20.0
        self.assertEqual(components["full_completion"]["points_delta"], 20.0)
        # R delta = (100-0) * 0.30 = 30.0 (was 0 before — full rhythm swing)
        self.assertEqual(components["rhythm_stability"]["points_delta"], 30.0)
        # M delta = 0 (weight unchanged; both windows are flat 100)
        self.assertEqual(components["recent_momentum"]["points_delta"], 0.0)

    def test_habit_score_drivers_identify_booster_drag_and_movement(self):
        start = date(2026, 1, 1)
        previous_start = start
        previous_end = start + timedelta(days=3)
        current_start = start + timedelta(days=4)
        current_end = start + timedelta(days=7)

        booster = self.make_habit(
            "Priority win",
            start,
            priority=Habit.PRIORITY_HIGH,
        )
        drag = self.make_habit(
            "Priority gap",
            start,
            priority=Habit.PRIORITY_HIGH,
        )
        improved = self.make_habit("Comeback", start)
        declined = self.make_habit("Needs reset", start)

        for offset in range(4):
            self.log_completion(booster, previous_start + timedelta(days=offset), 100)
            self.log_completion(booster, current_start + timedelta(days=offset), 100)
            self.log_completion(improved, previous_start + timedelta(days=offset), 0)
            self.log_completion(improved, current_start + timedelta(days=offset), 100)
            self.log_completion(declined, previous_start + timedelta(days=offset), 100)
            self.log_completion(declined, current_start + timedelta(days=offset), 0)

        drivers = build_habit_score_drivers(
            [booster, drag, improved, declined],
            current_start,
            current_end,
            previous_start,
            previous_end,
        )

        self.assertEqual(drivers["booster"]["habit"], booster)
        self.assertEqual(drivers["drag"]["habit"], drag)
        self.assertEqual(drivers["improved"]["habit"], improved)
        self.assertEqual(drivers["improved"]["score_delta"], 92.5)
        self.assertEqual(drivers["declined"]["habit"], declined)
        self.assertEqual(drivers["declined"]["score_delta"], -92.5)

    def test_category_analytics_marks_best_and_weakest_categories(self):
        start = date(2026, 1, 1)
        health = self.make_habit("Lift", start, categories=[self.categories["health"]])
        study = self.make_habit("Read", start, categories=[self.categories["study"]])
        spiritual = self.make_habit(
            "Reflect",
            start,
            categories=[self.categories["spiritual"]],
        )

        for offset in range(2):
            self.log_completion(health, start + timedelta(days=offset), 100)
            self.log_completion(study, start + timedelta(days=offset), 50)

        analytics = build_category_analytics(
            [health, study, spiritual],
            start,
            start + timedelta(days=1),
        )
        summaries = {item["key"]: item for item in analytics["summaries"]}

        self.assertEqual(len(analytics["summaries"]), len(DEFAULT_CATEGORIES))
        self.assertEqual(summaries["health"]["completion_rate"], 100.0)
        self.assertEqual(summaries["study"]["completion_rate"], 50.0)
        self.assertEqual(summaries["spiritual"]["completion_rate"], 0.0)
        self.assertEqual(analytics["best"]["key"], "health")
        self.assertEqual(analytics["weakest"]["key"], "spiritual")

    def test_today_page_has_date_picker_for_requested_date(self):
        self.client.force_login(self.user)

        response = self.client.get(reverse("habits:today"), {"date": "2026-06-03"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'type="date"')
        self.assertContains(response, 'name="date"')
        self.assertContains(response, 'value="2026-06-03"')

    def test_today_page_show_completed_toggle_persists_and_restores_items(self):
        today = date(2026, 5, 24)
        habit = Habit.objects.create(
            user=self.user,
            name="Toggle habit",
            habit_type=Habit.HABIT_BINARY,
            schedule_type=Habit.SCHEDULE_DAILY,
            start_date=today - timedelta(days=2),
        )
        HabitCompletion.objects.create(
            habit=habit,
            date=today,
            completion_percentage=Decimal("100"),
            raw_value=Decimal("100"),
        )

        self.client.force_login(self.user)

        hidden_response = self.client.get(
            reverse("habits:today"),
            {"date": today.isoformat(), "hide_completed": "1"},
        )
        self.assertEqual(hidden_response.status_code, 200)
        self.assertContains(hidden_response, "Show completed")
        self.assertNotContains(hidden_response, "data-progress-form")
        self.assertContains(hidden_response, "All scheduled habits are completed.")

        shown_response = self.client.get(
            reverse("habits:today"),
            {"date": today.isoformat(), "hide_completed": "0"},
        )
        self.assertEqual(shown_response.status_code, 200)
        self.assertContains(shown_response, "Hide completed")
        self.assertContains(shown_response, "data-progress-form")

    def test_today_page_inputs_enabled_for_today_and_yesterday_only(self):
        today = date(2026, 5, 24)
        habit = Habit.objects.create(
            user=self.user,
            name="Editable habit",
            habit_type=Habit.HABIT_PARTIAL,
            schedule_type=Habit.SCHEDULE_DAILY,
            start_date=today - timedelta(days=5),
        )

        self.client.force_login(self.user)

        with patch("habits.views.timezone.localdate", return_value=today):
            today_response = self.client.get(
                reverse("habits:today"),
                {"date": today.isoformat()},
            )
            self.assertEqual(today_response.status_code, 200)
            self.assertNotIn(
                "disabled",
                self._extract_progress_input(today_response, habit.id, "name=\"completion_percentage\""),
            )
            self.assertTrue(today_response.context["can_edit_progress"])

            yesterday_response = self.client.get(
                reverse("habits:today"),
                {"date": (today - timedelta(days=1)).isoformat()},
            )
            self.assertEqual(yesterday_response.status_code, 200)
            self.assertNotIn(
                "disabled",
                self._extract_progress_input(yesterday_response, habit.id, "name=\"completion_percentage\""),
            )
            self.assertTrue(yesterday_response.context["can_edit_progress"])

            older_response = self.client.get(
                reverse("habits:today"),
                {"date": (today - timedelta(days=3)).isoformat()},
            )
            self.assertEqual(older_response.status_code, 200)
            self.assertIn(
                "disabled",
                self._extract_progress_input(older_response, habit.id, "name=\"completion_percentage\""),
            )
            self.assertFalse(older_response.context["can_edit_progress"])

    def test_update_progress_rejects_dates_older_than_yesterday(self):
        today = date(2026, 5, 24)
        habit = Habit.objects.create(
            user=self.user,
            name="Old habit",
            habit_type=Habit.HABIT_PARTIAL,
            schedule_type=Habit.SCHEDULE_DAILY,
            start_date=today - timedelta(days=10),
        )

        self.client.force_login(self.user)

        with patch("habits.views.timezone.localdate", return_value=today):
            # Older than yesterday should be rejected.
            older = today - timedelta(days=2)
            rejected = self.client.post(
                reverse("habits:update_progress", args=[habit.id]),
                {
                    "date": older.isoformat(),
                    "completion_percentage": "75",
                    "next": reverse("habits:today"),
                },
            )
            self.assertEqual(rejected.status_code, 302)
            self.assertFalse(
                HabitCompletion.objects.filter(habit=habit, date=older).exists()
            )

            # Yesterday should still be allowed.
            yesterday = today - timedelta(days=1)
            allowed = self.client.post(
                reverse("habits:update_progress", args=[habit.id]),
                {
                    "date": yesterday.isoformat(),
                    "completion_percentage": "60",
                    "next": reverse("habits:today"),
                },
            )
            self.assertEqual(allowed.status_code, 302)
            self.assertTrue(
                HabitCompletion.objects.filter(habit=habit, date=yesterday).exists()
            )

    def test_update_progress_rejects_future_dates_without_mutating_data(self):
        today = date(2026, 5, 24)
        future = today + timedelta(days=1)
        habit = Habit.objects.create(
            user=self.user,
            name="Future habit",
            habit_type=Habit.HABIT_PARTIAL,
            schedule_type=Habit.SCHEDULE_DAILY,
            start_date=today,
        )
        existing = HabitCompletion.objects.create(
            habit=habit,
            date=future,
            completion_percentage=Decimal("0"),
            raw_value=Decimal("0"),
        )
        self.client.force_login(self.user)

        with patch("habits.views.timezone.localdate", return_value=today):
            response = self.client.post(
                reverse("habits:update_progress", args=[habit.id]),
                {
                    "date": future.isoformat(),
                    "completion_percentage": "100",
                    "next": "https://evil.example/",
                },
            )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("habits:today"))
        existing.refresh_from_db()
        self.assertEqual(existing.completion_percentage, Decimal("0"))

    def test_update_progress_rejects_missing_and_invalid_post_dates(self):
        today = date(2026, 5, 24)
        habit = Habit.objects.create(
            user=self.user,
            name="Strict date habit",
            habit_type=Habit.HABIT_PARTIAL,
            schedule_type=Habit.SCHEDULE_DAILY,
            start_date=today,
        )
        self.client.force_login(self.user)

        with patch("habits.views.timezone.localdate", return_value=today):
            for raw_date in (None, "not-a-date", "2026-02-30"):
                payload = {"completion_percentage": "100"}
                if raw_date is not None:
                    payload["date"] = raw_date
                response = self.client.post(
                    reverse("habits:update_progress", args=[habit.id]),
                    payload,
                )
                self.assertEqual(response.status_code, 302)

        self.assertFalse(
            HabitCompletion.objects.filter(habit=habit, date=today).exists()
        )

    def test_update_progress_uses_post_date_instead_of_query_string(self):
        today = date(2026, 5, 24)
        habit = Habit.objects.create(
            user=self.user,
            name="Posted date habit",
            habit_type=Habit.HABIT_PARTIAL,
            schedule_type=Habit.SCHEDULE_DAILY,
            start_date=today,
        )
        self.client.force_login(self.user)
        url = (
            reverse("habits:update_progress", args=[habit.id])
            + f"?date={(today + timedelta(days=5)).isoformat()}"
        )

        with patch("habits.views.timezone.localdate", return_value=today):
            response = self.client.post(
                url,
                {"date": today.isoformat(), "completion_percentage": "75"},
            )

        self.assertEqual(response.status_code, 302)
        completion = HabitCompletion.objects.get(habit=habit, date=today)
        self.assertEqual(completion.completion_percentage, Decimal("75"))

    def test_today_page_renders_completion_rate_in_summary(self):
        today = date(2026, 5, 24)
        scheduled_habit = Habit.objects.create(
            user=self.user,
            name="Done habit",
            habit_type=Habit.HABIT_BINARY,
            schedule_type=Habit.SCHEDULE_DAILY,
            start_date=today - timedelta(days=2),
        )
        HabitCompletion.objects.create(
            habit=scheduled_habit,
            date=today,
            completion_percentage=Decimal("100"),
            raw_value=Decimal("100"),
        )
        Habit.objects.create(
            user=self.user,
            name="Pending habit",
            habit_type=Habit.HABIT_BINARY,
            schedule_type=Habit.SCHEDULE_DAILY,
            start_date=today - timedelta(days=2),
        )

        self.client.force_login(self.user)
        response = self.client.get(
            reverse("habits:today"),
            {"date": today.isoformat()},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["completion_rate"], 50.0)
        self.assertContains(response, "scheduled-summary-desktop")
        self.assertContains(response, "50.0% completed")

    def test_today_page_completion_rate_weights_priority_and_partials(self):
        today = date(2026, 5, 24)
        # Weights mirror the Consistency score: Low=0.8, Medium=1.0, High=1.3
        # Completion: Low@100%, Medium@50%, High@0%
        # weighted_total = 0.8*100 + 1.0*50 + 1.3*0 = 130; weight_sum = 3.1
        # rate = round(130 / (3.1 * 100) * 100, 1) = 41.9
        low = Habit.objects.create(
            user=self.user,
            name="Low habit",
            habit_type=Habit.HABIT_BINARY,
            schedule_type=Habit.SCHEDULE_DAILY,
            start_date=today - timedelta(days=2),
            priority=Habit.PRIORITY_LOW,
        )
        HabitCompletion.objects.create(
            habit=low,
            date=today,
            completion_percentage=Decimal("100"),
            raw_value=Decimal("100"),
        )
        medium = Habit.objects.create(
            user=self.user,
            name="Medium habit",
            habit_type=Habit.HABIT_PARTIAL,
            schedule_type=Habit.SCHEDULE_DAILY,
            start_date=today - timedelta(days=2),
            priority=Habit.PRIORITY_MEDIUM,
        )
        HabitCompletion.objects.create(
            habit=medium,
            date=today,
            completion_percentage=Decimal("50"),
            raw_value=Decimal("50"),
        )
        Habit.objects.create(
            user=self.user,
            name="High habit",
            habit_type=Habit.HABIT_BINARY,
            schedule_type=Habit.SCHEDULE_DAILY,
            start_date=today - timedelta(days=2),
            priority=Habit.PRIORITY_HIGH,
        )

        self.client.force_login(self.user)
        response = self.client.get(
            reverse("habits:today"),
            {"date": today.isoformat()},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["completion_rate"], 41.9)
        self.assertContains(response, "41.9% completed")

    def test_weighted_partial_rate_is_consistent_across_site_features(self):
        today = date(2026, 5, 24)
        low = self.make_habit(
            "Low complete",
            today,
            priority=Habit.PRIORITY_LOW,
        )
        medium = self.make_habit(
            "Medium partial",
            today,
            priority=Habit.PRIORITY_MEDIUM,
        )
        high = self.make_habit(
            "High pending",
            today,
            priority=Habit.PRIORITY_HIGH,
        )
        self.log_completion(low, today, 100)
        self.log_completion(medium, today, 50)

        # (0.8 * 100 + 1.0 * 50 + 1.3 * 0) / (0.8 + 1.0 + 1.3)
        expected_rate = 41.9
        self.client.force_login(self.user)

        with patch("habits.views.timezone.localdate", return_value=today), patch(
            "habits.services.timezone.localdate",
            return_value=today,
        ):
            today_response = self.client.get(reverse("habits:today"))
            dashboard_response = self.client.get(reverse("habits:dashboard"))
            profile_response = self.client.get(
                reverse("habits:user_profile", args=[self.user.username])
            )
            reports_response = self.client.get(reverse("habits:reports"))
            leaderboard_response = self.client.get(reverse("habits:leaderboard"))
            compare_response = self.client.get(
                reverse("habits:habit_compare"),
                {"habit_ids": [low.id, medium.id, high.id]},
            )

        category_summaries = {
            item["key"]: item
            for item in dashboard_response.context["category_analytics"]["summaries"]
        }
        leaderboard_entry = leaderboard_response.context["leaderboard_entries"][0]
        comparison_rates = {
            row["habit"].id: row["metrics_90"]["completion_rate"]
            for row in compare_response.context["comparison_rows"]
        }

        self.assertEqual(today_response.context["completion_rate"], expected_rate)
        self.assertEqual(dashboard_response.context["overall_rate"], expected_rate)
        self.assertEqual(
            json.loads(dashboard_response.context["chart_rates"])[-1],
            expected_rate,
        )
        self.assertEqual(
            category_summaries["spiritual"]["completion_rate"],
            expected_rate,
        )
        self.assertEqual(profile_response.context["overall_completion"], expected_rate)
        self.assertEqual(
            json.loads(profile_response.context["daily_rates"])[-1],
            expected_rate,
        )
        self.assertEqual(
            profile_response.context["monthly_reports"][-1]["completion_rate"],
            expected_rate,
        )
        self.assertEqual(
            reports_response.context["weekly_reports"][-1]["completion_rate"],
            expected_rate,
        )
        self.assertEqual(
            reports_response.context["monthly_reports"][-1]["completion_rate"],
            expected_rate,
        )
        self.assertEqual(leaderboard_entry["overall_completion"], expected_rate)
        self.assertEqual(comparison_rates[low.id], 100.0)
        self.assertEqual(comparison_rates[medium.id], 50.0)
        self.assertEqual(comparison_rates[high.id], 0.0)

    def test_today_and_dashboard_both_count_unlogged_current_sessions(self):
        today = date(2026, 5, 24)
        completed = Habit.objects.create(
            user=self.user,
            name="Current complete",
            habit_type=Habit.HABIT_PARTIAL,
            schedule_type=Habit.SCHEDULE_DAILY,
            priority=Habit.PRIORITY_MEDIUM,
            start_date=today,
        )
        Habit.objects.create(
            user=self.user,
            name="Current pending",
            habit_type=Habit.HABIT_PARTIAL,
            schedule_type=Habit.SCHEDULE_DAILY,
            priority=Habit.PRIORITY_MEDIUM,
            start_date=today,
        )
        HabitCompletion.objects.create(
            habit=completed,
            date=today,
            completion_percentage=Decimal("100"),
            raw_value=Decimal("100"),
        )
        self.client.force_login(self.user)

        with patch("habits.views.timezone.localdate", return_value=today), patch(
            "habits.services.timezone.localdate",
            return_value=today,
        ):
            today_response = self.client.get(reverse("habits:today"))
            dashboard_response = self.client.get(reverse("habits:dashboard"))
            profile_response = self.client.get(
                reverse("habits:user_profile", args=[self.user.username])
            )
            reports_response = self.client.get(reverse("habits:reports"))

        self.assertEqual(today_response.context["completion_rate"], 50.0)
        self.assertEqual(dashboard_response.context["overall_rate"], 50.0)
        self.assertEqual(dashboard_response.context["total_scheduled"], 2)
        self.assertEqual(json.loads(dashboard_response.context["chart_rates"])[-1], 50.0)
        self.assertEqual(profile_response.context["overall_completion"], 50.0)
        self.assertEqual(json.loads(profile_response.context["daily_rates"])[-1], 50.0)
        self.assertEqual(
            reports_response.context["weekly_reports"][-1]["completion_rate"],
            50.0,
        )

    def _extract_progress_input(self, response, habit_id, name):
        from django.urls import reverse
        needle = reverse("habits:update_progress", args=[habit_id])
        content = response.content.decode("utf-8")
        form_start = content.find(f'action="{needle}"')
        self.assertGreater(form_start, -1, "Expected progress form for habit")
        slice_end = content.find("</form>", form_start)
        form_chunk = content[form_start:slice_end]
        attr_index = form_chunk.find(name)
        self.assertGreater(attr_index, -1, f"Expected attribute {name}")
        tag_start = form_chunk.rfind("<", 0, attr_index)
        tag_end = form_chunk.find(">", attr_index)
        return form_chunk[tag_start:tag_end]

    def test_mobile_all_habits_page_renders_shared_all_habits_section(self):
        habit = Habit.objects.create(
            user=self.user,
            name="Mobile list habit",
            habit_type=Habit.HABIT_BINARY,
            schedule_type=Habit.SCHEDULE_DAILY,
            start_date=date(2026, 5, 1),
        )

        self.client.force_login(self.user)

        today_response = self.client.get(reverse("habits:today"))
        self.assertEqual(today_response.status_code, 200)
        self.assertContains(today_response, "All Habits")
        self.assertContains(today_response, "Pause all habits")
        self.assertContains(today_response, reverse("habits:pause_all_habits"))
        self.assertNotContains(today_response, "Back to today")

        mobile_response = self.client.get(reverse("habits:mobile_all_habits"))
        self.assertEqual(mobile_response.status_code, 200)
        self.assertContains(mobile_response, "Mobile menu")
        self.assertContains(mobile_response, "Mobile list habit")
        self.assertContains(mobile_response, "Pause all habits")
        self.assertContains(mobile_response, reverse("habits:pause_all_habits"))
        self.assertContains(mobile_response, "habitSortList")
        self.assertContains(mobile_response, 'enableHabitDragSort("habitSortList")')
        self.assertNotContains(mobile_response, "Back to today")

    def test_dashboard_renders_score_drivers_and_category_analytics(self):
        today = date(2026, 5, 10)
        habit = self.make_habit(
            "Dashboard habit",
            date(2026, 5, 2),
            categories=[self.categories["health"]],
        )
        for offset in range(8):
            self.log_completion(habit, habit.start_date + timedelta(days=offset), 100)

        self.client.force_login(self.user)
        with patch("habits.views.timezone.localdate", return_value=today), patch(
            "habits.services.timezone.localdate",
            return_value=today,
        ):
            response = self.client.get(reverse("habits:dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Score breakdown")
        self.assertContains(response, "Biggest score booster")
        self.assertContains(response, "Category analytics")
        self.assertContains(response, "Last 30 days")

    def test_dashboard_uses_full_rolling_window_before_may_10(self):
        today = date(2026, 5, 17)
        habit = self.make_habit("Older dashboard habit", date(2026, 4, 18))
        self.log_completion(habit, date(2026, 4, 18), 100)

        self.client.force_login(self.user)
        with patch("habits.views.timezone.localdate", return_value=today), patch(
            "habits.services.timezone.localdate",
            return_value=today,
        ):
            response = self.client.get(reverse("habits:dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Apr 18 - May 17")

    def test_reports_use_section_periods_before_may_10(self):
        today = date(2026, 5, 17)
        habit = self.make_habit("April habit", date(2026, 4, 1))

        with patch("habits.services.timezone.localdate", return_value=today):
            weekly_reports = build_weekly_reports([habit], weeks=2, today=today)
            monthly_reports = build_monthly_reports([habit], months=2, today=today)

        self.assertEqual(
            [report["label"] for report in weekly_reports],
            ["May 04 - May 10", "May 11 - May 17"],
        )
        self.assertEqual(weekly_reports[0]["total_scheduled"], 7)
        self.assertEqual(
            [report["label"] for report in monthly_reports],
            ["Apr 2026", "May 2026"],
        )
        self.assertEqual(monthly_reports[0]["total_scheduled"], 30)

    def test_habit_detail_uses_all_time_stats_and_limits_history(self):
        today = date(2026, 5, 17)
        habit = self.make_habit("All time detail habit", date(2026, 4, 1))
        for offset in range(17):
            self.log_completion(habit, habit.start_date + timedelta(days=offset), 100)

        # This test is about the detail page, not the recap prompt. Yesterday's
        # recap is genuinely unfinished here, so record it as done to keep the
        # blocking overlay (and its extra body class) out of the assertions.
        DailyRecapCompletion.objects.create(
            user=self.user,
            date=today - timedelta(days=1),
        )

        self.client.force_login(self.user)

        with patch("habits.views.timezone.localdate", return_value=today), patch(
            "habits.services.timezone.localdate",
            return_value=today,
        ):
            response = self.client.get(reverse("habits:habit_detail", args=[habit.id]))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["stats"]["completion_rate"], 0.0)
        self.assertEqual(response.context["all_time_stats"]["completion_rate"], 36.2)
        self.assertEqual(len(response.context["history"]), 15)
        self.assertContains(response, "All time")
        self.assertContains(response, "Habit focus")
        self.assertContains(response, 'class="habit-detail-page"')
        self.assertContains(response, "Swipe to see more")
        self.assertContains(response, "15 sessions")

    def test_paused_dates_are_excluded_from_metrics(self):
        start = date(2026, 1, 1)
        habit = self.make_habit("Pause test", start)
        self.log_completion(habit, start, 100)
        self.log_completion(habit, start + timedelta(days=1), 100)
        self.log_completion(habit, start + timedelta(days=2), 100)

        HabitPause.objects.create(
            habit=habit,
            start_date=start + timedelta(days=1),
            end_date=start + timedelta(days=3),
        )

        metrics = habit_performance_metrics(habit, start, start + timedelta(days=2))

        self.assertEqual(metrics["scheduled_total"], 1)
        self.assertEqual(metrics["completed_total"], 1)
        self.assertEqual(metrics["completion_rate"], 100.0)

    def test_pause_habit_starts_tomorrow(self):
        today = date(2026, 2, 2)
        habit = self.make_habit("Pause tomorrow", today - timedelta(days=1))

        self.client.force_login(self.user)
        with patch("habits.views.timezone.localdate", return_value=today):
            self.client.post(reverse("habits:pause_habit", args=[habit.id]))

        pause = HabitPause.objects.get(habit=habit, end_date__isnull=True)
        self.assertEqual(pause.start_date, today + timedelta(days=1))
        self.assertFalse(habit.is_paused_on(today))
        self.assertTrue(habit.is_paused_on(today + timedelta(days=1)))

    def test_pause_all_habits_starts_tomorrow_for_user_habits(self):
        today = date(2026, 2, 2)
        first = self.make_habit("Pause all first", today - timedelta(days=1))
        second = self.make_habit("Pause all second", today - timedelta(days=1))
        already_paused = self.make_habit("Already paused", today - timedelta(days=1))
        HabitPause.objects.create(habit=already_paused, start_date=today)
        other_user = get_user_model().objects.create_user(
            username="other-pause-user",
            password="not-used",
        )
        other_habit = Habit.objects.create(
            user=other_user,
            name="Other user's habit",
            habit_type=Habit.HABIT_BINARY,
            schedule_type=Habit.SCHEDULE_DAILY,
            start_date=today - timedelta(days=1),
        )

        self.client.force_login(self.user)
        with patch("habits.views.timezone.localdate", return_value=today):
            response = self.client.post(
                reverse("habits:pause_all_habits"),
                {"next": reverse("habits:today")},
            )

        self.assertRedirects(
            response,
            reverse("habits:today"),
            fetch_redirect_response=False,
        )
        for habit in (first, second):
            pause = HabitPause.objects.get(habit=habit, end_date__isnull=True)
            self.assertEqual(pause.start_date, today + timedelta(days=1))
            self.assertFalse(habit.is_paused_on(today))
            self.assertTrue(habit.is_paused_on(today + timedelta(days=1)))

        existing_pause = HabitPause.objects.get(habit=already_paused)
        self.assertEqual(existing_pause.start_date, today)
        self.assertFalse(HabitPause.objects.filter(habit=other_habit).exists())

    def test_resume_makes_today_schedulable(self):
        today = date(2026, 2, 2)
        habit = self.make_habit("Resume test", today - timedelta(days=1))

        pause = HabitPause.objects.create(habit=habit, start_date=today)
        self.assertFalse(habit.is_scheduled_on(today))

        pause.end_date = today
        pause.save(update_fields=["end_date", "updated_at"])
        self.assertTrue(habit.is_scheduled_on(today))


class FriendRequestFeatureTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.alice = User.objects.create_user(
            username="alice",
            password="not-used",
        )
        self.bob = User.objects.create_user(
            username="bob",
            password="not-used",
        )
        self.cara = User.objects.create_user(
            username="cara",
            password="not-used",
        )

    def test_search_can_send_friend_request(self):
        self.client.force_login(self.alice)
        search_url = f"{reverse('habits:user_search')}?q=bob"

        response = self.client.get(search_url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "bob")
        self.assertContains(response, "Send request")

        response = self.client.post(
            reverse("habits:send_friend_request", args=[self.bob.id]),
            {"next": search_url},
        )

        self.assertRedirects(response, search_url, fetch_redirect_response=False)
        friend_request = FriendRequest.objects.get(
            from_user=self.alice,
            to_user=self.bob,
        )
        self.assertEqual(friend_request.status, FriendRequest.STATUS_PENDING)

    def test_search_can_accept_incoming_friend_request(self):
        friend_request = FriendRequest.objects.create(
            from_user=self.bob,
            to_user=self.alice,
        )
        self.client.force_login(self.alice)
        search_url = f"{reverse('habits:user_search')}?q=bob"

        response = self.client.get(search_url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Sent you a friend request")
        self.assertContains(response, "Accept request")

        response = self.client.post(
            reverse("habits:accept_friend_request", args=[friend_request.id]),
            {"next": search_url},
        )

        self.assertRedirects(response, search_url, fetch_redirect_response=False)
        friend_request.refresh_from_db()
        self.assertEqual(friend_request.status, FriendRequest.STATUS_ACCEPTED)

    def test_notification_panel_lists_incoming_friend_requests(self):
        FriendRequest.objects.create(from_user=self.bob, to_user=self.alice)
        self.client.force_login(self.alice)

        response = self.client.get(reverse("habits:today"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "notification-badge")
        self.assertContains(response, "bob")
        self.assertContains(response, "Accept")

    def test_username_profile_url_renders_target_profile(self):
        self.client.force_login(self.alice)

        response = self.client.get(
            reverse("habits:user_profile", args=[self.bob.username])
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "<h1>bob</h1>", html=True)
        self.assertContains(response, "Viewing bob's progress profile.")

    def test_legacy_profile_url_redirects_to_current_username(self):
        self.client.force_login(self.cara)

        response = self.client.get(reverse("habits:profile"))

        self.assertRedirects(
            response,
            reverse("habits:user_profile", args=[self.cara.username]),
            fetch_redirect_response=False,
        )

    def test_leaderboard_ranks_current_user_and_accepted_friends(self):
        FriendRequest.objects.create(
            from_user=self.alice,
            to_user=self.bob,
            status=FriendRequest.STATUS_ACCEPTED,
        )
        FriendRequest.objects.create(from_user=self.alice, to_user=self.cara)
        habit = Habit.objects.create(
            user=self.bob,
            name="Bob daily",
            habit_type=Habit.HABIT_BINARY,
            schedule_type=Habit.SCHEDULE_DAILY,
            start_date=date(2026, 5, 15),
        )
        for target_date in (date(2026, 5, 15), date(2026, 5, 16)):
            HabitCompletion.objects.create(
                habit=habit,
                date=target_date,
                completion_percentage=Decimal("100"),
                raw_value=Decimal("100"),
            )
        self.client.force_login(self.alice)

        with patch("habits.views.timezone.localdate", return_value=date(2026, 5, 17)), patch(
            "habits.services.timezone.localdate",
            return_value=date(2026, 5, 17),
        ):
            response = self.client.get(reverse("habits:leaderboard"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Leaderboard")
        self.assertContains(response, "Current window")
        self.assertContains(response, "All time")
        self.assertContains(response, "alice")
        self.assertContains(response, "bob")
        self.assertNotContains(response, "cara")

        content = response.content.decode()
        ranking_markup = content.split('<div class="leaderboard-list">', 1)[1]
        self.assertLess(ranking_markup.index("bob"), ranking_markup.index("alice"))

    def test_leaderboard_can_switch_between_current_window_and_all_time(self):
        FriendRequest.objects.create(
            from_user=self.alice,
            to_user=self.bob,
            status=FriendRequest.STATUS_ACCEPTED,
        )
        habit = Habit.objects.create(
            user=self.bob,
            name="Old win",
            habit_type=Habit.HABIT_BINARY,
            schedule_type=Habit.SCHEDULE_INTERVAL,
            interval_days=999,
            start_date=date(2026, 4, 10),
        )
        HabitCompletion.objects.create(
            habit=habit,
            date=date(2026, 4, 10),
            completion_percentage=Decimal("100"),
            raw_value=Decimal("100"),
        )
        self.client.force_login(self.alice)

        with patch("habits.views.timezone.localdate", return_value=date(2026, 5, 17)), patch(
            "habits.services.timezone.localdate",
            return_value=date(2026, 5, 17),
        ):
            current_window_response = self.client.get(reverse("habits:leaderboard"))
            all_time_response = self.client.get(
                f"{reverse('habits:leaderboard')}?window=all"
            )

        current_window_ranking = current_window_response.content.decode().split(
            '<div class="leaderboard-list">',
            1,
        )[1]
        all_time_ranking = all_time_response.content.decode().split(
            '<div class="leaderboard-list">',
            1,
        )[1]

        self.assertContains(current_window_response, "Apr 18 - May 17")
        self.assertContains(all_time_response, "All tracked history")
        self.assertLess(
            current_window_ranking.index("alice"),
            current_window_ranking.index("bob"),
        )
        self.assertLess(all_time_ranking.index("bob"), all_time_ranking.index("alice"))


class DailyRecapPromptTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="recap-user",
            password="not-used",
        )

    def test_daily_recap_prompts_on_first_authenticated_request_after_midnight(self):
        today = date(2026, 5, 23)
        yesterday = today - timedelta(days=1)
        habit = Habit.objects.create(
            user=self.user,
            name="Unfinished daily",
            habit_type=Habit.HABIT_BINARY,
            schedule_type=Habit.SCHEDULE_DAILY,
            start_date=yesterday,
        )
        HabitCompletion.objects.create(
            habit=habit,
            date=yesterday,
            completion_percentage=Decimal("0"),
            raw_value=Decimal("0"),
        )

        self.client.force_login(self.user)
        self.user.last_login = timezone.make_aware(datetime(2026, 5, 22, 23, 45))
        self.user.save(update_fields=["last_login"])

        original_localdate = timezone.localdate

        def fake_localdate(value=None):
            if value is None:
                return today
            return original_localdate(value)

        with patch("habits.context_processors.timezone.localdate", side_effect=fake_localdate):
            response = self.client.get(reverse("habits:today"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Finish yesterday's check-ins")
        self.assertContains(response, "Unfinished daily")
        self.assertEqual(self.client.session["daily_recap_date"], yesterday.isoformat())

    def test_daily_recap_shows_target_inside_quantitative_input(self):
        """Quantitative rows mirror the desktop "value / max" combined field."""
        today = date(2026, 5, 23)
        yesterday = today - timedelta(days=1)
        habit = Habit.objects.create(
            user=self.user,
            name="Drink water",
            habit_type=Habit.HABIT_QUANTITATIVE,
            schedule_type=Habit.SCHEDULE_DAILY,
            start_date=yesterday,
            target_value=Decimal("10"),
            unit="glasses",
        )
        HabitCompletion.objects.create(
            habit=habit,
            date=yesterday,
            completion_percentage=Decimal("60"),
            raw_value=Decimal("6"),
        )

        self.client.force_login(self.user)
        self.user.last_login = timezone.make_aware(datetime(2026, 5, 22, 23, 45))
        self.user.save(update_fields=["last_login"])

        original_localdate = timezone.localdate

        def fake_localdate(value=None):
            if value is None:
                return today
            return original_localdate(value)

        with patch(
            "habits.context_processors.timezone.localdate", side_effect=fake_localdate
        ):
            response = self.client.get(reverse("habits:today"))

        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        recap = content.split('class="daily-recap-overlay"', 1)[1]

        # The editable value and the max live in the same bordered field, and
        # the input still clamps to the target.
        self.assertIn('class="quant-field"', recap)
        self.assertIn('<span class="quant-suffix">/ 10 glasses</span>', recap)
        self.assertIn('max="10"', recap)
        self.assertIn('value="6"', recap)

    def test_daily_recap_rejects_direct_posts_without_server_session(self):
        today = date(2026, 5, 23)
        old_date = today - timedelta(days=20)
        habit = Habit.objects.create(
            user=self.user,
            name="Protected history",
            habit_type=Habit.HABIT_BINARY,
            schedule_type=Habit.SCHEDULE_DAILY,
            start_date=old_date,
        )
        completion = HabitCompletion.objects.create(
            habit=habit,
            date=old_date,
            completion_percentage=Decimal("0"),
            raw_value=Decimal("0"),
        )
        self.client.force_login(self.user)

        with patch("habits.views.timezone.localdate", return_value=today):
            response = self.client.post(
                reverse("habits:daily_recap"),
                {
                    "date": old_date.isoformat(),
                    f"completed_{habit.id}": "1",
                    "next": "https://evil.example/",
                },
            )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("habits:today"))
        completion.refresh_from_db()
        self.assertEqual(completion.completion_percentage, Decimal("0"))
        self.assertFalse(
            DailyRecapCompletion.objects.filter(user=self.user).exists()
        )

    def test_daily_recap_rejects_stale_and_future_session_dates(self):
        today = date(2026, 5, 23)
        habit = Habit.objects.create(
            user=self.user,
            name="Expired recap",
            habit_type=Habit.HABIT_BINARY,
            schedule_type=Habit.SCHEDULE_DAILY,
            start_date=today - timedelta(days=10),
        )
        self.client.force_login(self.user)

        with patch("habits.views.timezone.localdate", return_value=today):
            for target_date in (
                today - timedelta(days=2),
                today + timedelta(days=1),
            ):
                session = self.client.session
                session["daily_recap_date"] = target_date.isoformat()
                session.save()
                response = self.client.post(
                    reverse("habits:daily_recap"),
                    {
                        "date": target_date.isoformat(),
                        f"completed_{habit.id}": "1",
                    },
                )
                self.assertEqual(response.status_code, 302)
                self.assertNotIn("daily_recap_date", self.client.session)
                self.assertFalse(
                    DailyRecapCompletion.objects.filter(user=self.user).exists()
                )

        self.assertFalse(HabitCompletion.objects.filter(habit=habit).exists())

    def test_daily_recap_rejects_a_date_that_does_not_match_session(self):
        today = date(2026, 5, 23)
        yesterday = today - timedelta(days=1)
        habit = Habit.objects.create(
            user=self.user,
            name="Mismatch protection",
            habit_type=Habit.HABIT_BINARY,
            schedule_type=Habit.SCHEDULE_DAILY,
            start_date=yesterday,
        )
        self.client.force_login(self.user)
        session = self.client.session
        session["daily_recap_date"] = yesterday.isoformat()
        session.save()

        with patch("habits.views.timezone.localdate", return_value=today):
            response = self.client.post(
                reverse("habits:daily_recap"),
                {
                    "date": (yesterday - timedelta(days=1)).isoformat(),
                    f"completed_{habit.id}": "1",
                },
            )

        self.assertEqual(response.status_code, 302)
        self.assertFalse(HabitCompletion.objects.filter(habit=habit).exists())
        self.assertNotIn("daily_recap_date", self.client.session)
        self.assertFalse(
            DailyRecapCompletion.objects.filter(user=self.user).exists()
        )

    def test_daily_recap_accepts_matching_server_issued_yesterday(self):
        today = date(2026, 5, 23)
        yesterday = today - timedelta(days=1)
        habit = Habit.objects.create(
            user=self.user,
            name="Valid recap",
            habit_type=Habit.HABIT_BINARY,
            schedule_type=Habit.SCHEDULE_DAILY,
            start_date=yesterday,
        )
        self.client.force_login(self.user)
        session = self.client.session
        session["daily_recap_date"] = yesterday.isoformat()
        session.save()

        with patch("habits.views.timezone.localdate", return_value=today):
            response = self.client.post(
                reverse("habits:daily_recap"),
                {
                    "date": yesterday.isoformat(),
                    f"completed_{habit.id}": "1",
                },
            )

        self.assertEqual(response.status_code, 302)
        completion = HabitCompletion.objects.get(habit=habit, date=yesterday)
        self.assertEqual(completion.completion_percentage, Decimal("100"))
        self.assertNotIn("daily_recap_date", self.client.session)
        # The finished recap is recorded in the database, not the session, so it
        # is visible to every other device, browser, and session.
        self.assertTrue(
            DailyRecapCompletion.objects.filter(
                user=self.user,
                date=yesterday,
            ).exists()
        )

    def test_daily_recap_prompt_replaces_a_stale_session_date(self):
        today = date(2026, 5, 23)
        yesterday = today - timedelta(days=1)
        Habit.objects.create(
            user=self.user,
            name="Fresh recap target",
            habit_type=Habit.HABIT_BINARY,
            schedule_type=Habit.SCHEDULE_DAILY,
            start_date=yesterday,
        )
        self.client.force_login(self.user)
        self.user.last_login = timezone.make_aware(datetime(2026, 5, 22, 23, 45))
        self.user.save(update_fields=["last_login"])
        session = self.client.session
        session["daily_recap_date"] = (today - timedelta(days=3)).isoformat()
        session.save()

        original_localdate = timezone.localdate

        def fake_localdate(value=None):
            if value is None:
                return today
            return original_localdate(value)

        with patch("habits.context_processors.timezone.localdate", side_effect=fake_localdate):
            response = self.client.get(reverse("habits:today"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.session["daily_recap_date"], yesterday.isoformat())
        self.assertContains(response, "May 22, 2026")


class DailyRecapMultiDeviceTests(TestCase):
    """The prompt must be resolved from database state, not per-device state.

    Submitting "Save and continue" can legitimately persist completions below
    100% (an unchecked binary habit stores 0%), so the pending-habit check alone
    can never tell whether the user already answered the prompt. These tests
    pin the behaviour to the persisted recap record so the answer is identical
    on every device, browser, session, and repeated login.
    """

    TODAY = date(2026, 5, 23)
    YESTERDAY = date(2026, 5, 22)
    PREVIOUS_LOGIN = datetime(2026, 5, 22, 23, 45)

    def setUp(self):
        self.password = "multi-device-pass-123"
        self.user = get_user_model().objects.create_user(
            username="multi-device-user",
            password=self.password,
        )

    def _make_habit(self, name, habit_type=Habit.HABIT_BINARY, target_value=None):
        return Habit.objects.create(
            user=self.user,
            name=name,
            habit_type=habit_type,
            target_value=target_value,
            schedule_type=Habit.SCHEDULE_DAILY,
            start_date=self.YESTERDAY,
        )

    def _stale_login(self):
        """Make ``last_login`` land on the previous day so the prompt is due."""
        self.user.last_login = timezone.make_aware(self.PREVIOUS_LOGIN)
        self.user.save(update_fields=["last_login"])

    def _fake_localdate(self):
        original_localdate = timezone.localdate

        def fake_localdate(value=None):
            if value is None:
                return self.TODAY
            return original_localdate(value)

        return fake_localdate

    def _new_device(self):
        """Return a client representing a separate device or browser."""
        from django.test import Client

        return Client()

    def _load_today(self, client):
        """Fetch the today page with the clock pinned, as a device would."""
        with patch(
            "habits.context_processors.timezone.localdate",
            side_effect=self._fake_localdate(),
        ):
            return client.get(reverse("habits:today"))

    def _login(self, client):
        with patch(
            "habits.views.timezone.localdate",
            return_value=self.TODAY,
        ), patch(
            "habits.services.timezone.localdate",
            return_value=self.TODAY,
        ):
            return client.post(
                reverse("habits:login"),
                {"username": self.user.username, "password": self.password},
            )

    def _submit_recap(self, client, payload):
        with patch("habits.views.timezone.localdate", return_value=self.TODAY):
            return client.post(
                reverse("habits:daily_recap"),
                {"date": self.YESTERDAY.isoformat(), **payload},
            )

    def _finish_recap_on(self, client, payload):
        """Show and submit the recap on one device, as a real user would."""
        self._stale_login()
        prompt = self._load_today(client)
        self.assertContains(prompt, "Finish yesterday's check-ins")
        response = self._submit_recap(client, payload)
        self.assertEqual(response.status_code, 302)
        return response

    def test_second_device_does_not_reprompt_after_save_and_continue(self):
        # An unchecked binary habit is saved as 0%, so it stays "pending" while
        # the recap itself is finished. This is the reported bug.
        habit = self._make_habit("Left unchecked")
        device_a = self._new_device()
        device_a.force_login(self.user)

        self._finish_recap_on(device_a, {})

        completion = HabitCompletion.objects.get(habit=habit, date=self.YESTERDAY)
        self.assertEqual(completion.completion_percentage, Decimal("0"))
        self.assertTrue(
            DailyRecapCompletion.objects.filter(
                user=self.user,
                date=self.YESTERDAY,
            ).exists()
        )

        device_b = self._new_device()
        device_b.force_login(self.user)
        self._stale_login()
        response = self._load_today(device_b)

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.context["daily_recap"])
        self.assertNotContains(response, "Finish yesterday's check-ins")
        self.assertNotIn("daily_recap_date", device_b.session)

    def test_partial_progress_still_hides_the_prompt_on_other_devices(self):
        habit = self._make_habit("Partial habit", habit_type=Habit.HABIT_PARTIAL)
        device_a = self._new_device()
        device_a.force_login(self.user)

        self._finish_recap_on(device_a, {f"percentage_{habit.id}": "60"})

        completion = HabitCompletion.objects.get(habit=habit, date=self.YESTERDAY)
        self.assertEqual(completion.completion_percentage, Decimal("60"))

        device_b = self._new_device()
        device_b.force_login(self.user)
        self._stale_login()
        response = self._load_today(device_b)

        self.assertIsNone(response.context["daily_recap"])
        self.assertNotContains(response, "Finish yesterday's check-ins")

    def test_quantitative_partial_progress_hides_the_prompt_elsewhere(self):
        habit = self._make_habit(
            "Quantitative habit",
            habit_type=Habit.HABIT_QUANTITATIVE,
            target_value=Decimal("10"),
        )
        device_a = self._new_device()
        device_a.force_login(self.user)

        self._finish_recap_on(device_a, {f"value_{habit.id}": "4"})

        completion = HabitCompletion.objects.get(habit=habit, date=self.YESTERDAY)
        self.assertEqual(completion.raw_value, Decimal("4"))
        self.assertLess(completion.completion_percentage, Decimal("100"))

        device_b = self._new_device()
        device_b.force_login(self.user)
        self._stale_login()
        response = self._load_today(device_b)

        self.assertIsNone(response.context["daily_recap"])

    def test_fresh_login_on_a_new_browser_does_not_reprompt(self):
        # A brand new browser authenticates for the first time today, so it has
        # no prior session at all and must rely purely on database state.
        self._make_habit("Left unchecked")
        device_a = self._new_device()
        device_a.force_login(self.user)
        self._finish_recap_on(device_a, {})

        self._stale_login()
        browser_b = self._new_device()
        login_response = self._login(browser_b)

        self.assertEqual(login_response.status_code, 302)
        self.assertNotIn("daily_recap_date", browser_b.session)

        response = self._load_today(browser_b)
        self.assertIsNone(response.context["daily_recap"])
        self.assertNotContains(response, "Finish yesterday's check-ins")

    def test_login_still_prompts_when_the_recap_is_unfinished(self):
        self._make_habit("Genuinely pending")
        self._stale_login()
        browser = self._new_device()

        self._login(browser)
        response = self._load_today(browser)

        self.assertEqual(
            browser.session["daily_recap_date"],
            self.YESTERDAY.isoformat(),
        )
        self.assertIsNotNone(response.context["daily_recap"])
        self.assertContains(response, "Finish yesterday's check-ins")

    def test_repeated_logins_and_reloads_stay_hidden_once_finished(self):
        self._make_habit("Left unchecked")
        device_a = self._new_device()
        device_a.force_login(self.user)
        self._finish_recap_on(device_a, {})

        for attempt in range(3):
            with self.subTest(attempt=attempt):
                client = self._new_device()
                self._stale_login()
                self._login(client)
                first_load = self._load_today(client)
                second_load = self._load_today(client)

                self.assertIsNone(first_load.context["daily_recap"])
                self.assertIsNone(second_load.context["daily_recap"])
                self.assertNotIn("daily_recap_date", client.session)

    def test_concurrent_sessions_for_one_user_agree_on_visibility(self):
        habit = self._make_habit("Left unchecked")
        session_one = self._new_device()
        session_two = self._new_device()
        session_one.force_login(self.user)
        session_two.force_login(self.user)

        # Both sessions render the prompt before either submits.
        self._stale_login()
        self.assertContains(
            self._load_today(session_one),
            "Finish yesterday's check-ins",
        )
        self.assertContains(
            self._load_today(session_two),
            "Finish yesterday's check-ins",
        )

        self._submit_recap(session_one, {f"completed_{habit.id}": "1"})

        self._stale_login()
        response = self._load_today(session_two)
        self.assertIsNone(response.context["daily_recap"])
        self.assertNotContains(response, "Finish yesterday's check-ins")

    def test_recap_completion_is_scoped_per_user(self):
        other_user = get_user_model().objects.create_user(
            username="other-recap-user",
            password="not-used",
        )
        Habit.objects.create(
            user=other_user,
            name="Other pending habit",
            habit_type=Habit.HABIT_BINARY,
            schedule_type=Habit.SCHEDULE_DAILY,
            start_date=self.YESTERDAY,
        )
        self._make_habit("Left unchecked")
        device_a = self._new_device()
        device_a.force_login(self.user)
        self._finish_recap_on(device_a, {})

        other_client = self._new_device()
        other_client.force_login(other_user)
        other_user.last_login = timezone.make_aware(self.PREVIOUS_LOGIN)
        other_user.save(update_fields=["last_login"])
        response = self._load_today(other_client)

        self.assertIsNotNone(response.context["daily_recap"])
        self.assertContains(response, "Finish yesterday's check-ins")

    def test_a_new_days_recap_prompts_again_after_yesterdays_was_finished(self):
        self._make_habit("Left unchecked")
        DailyRecapCompletion.objects.create(
            user=self.user,
            date=self.YESTERDAY - timedelta(days=1),
        )
        client = self._new_device()
        client.force_login(self.user)
        self._stale_login()

        response = self._load_today(client)

        self.assertIsNotNone(response.context["daily_recap"])
        self.assertEqual(response.context["daily_recap"]["date"], self.YESTERDAY)

    def test_duplicate_recap_submissions_remain_idempotent(self):
        habit = self._make_habit("Left unchecked")
        client = self._new_device()
        client.force_login(self.user)
        self._finish_recap_on(client, {f"completed_{habit.id}": "1"})

        # Replaying the submission has no server-issued session date, so it is
        # rejected without creating a duplicate record.
        self._submit_recap(client, {f"completed_{habit.id}": "1"})

        self.assertEqual(
            DailyRecapCompletion.objects.filter(
                user=self.user,
                date=self.YESTERDAY,
            ).count(),
            1,
        )
