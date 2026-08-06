import json
from datetime import date, datetime, timedelta
from decimal import Decimal
from math import sqrt
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import (
    DEFAULT_CATEGORIES,
    FriendRequest,
    Habit,
    HabitCategory,
    HabitCompletion,
    HabitPause,
)
from .services import (
    build_category_analytics,
    build_habit_score_drivers,
    build_monthly_reports,
    build_overall_score_breakdown,
    build_weekly_reports,
    calculate_overall_consistency,
    compute_user_metrics,
    daily_average_completion_series,
    habit_performance_metrics,
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
            4,
        )
        self.assertContains(response, "largest weighted share")
        self.assertContains(response, "largest weighted gap")
        self.assertContains(response, "largest increase in Consistency Score")
        self.assertContains(response, "largest decrease in Consistency Score")

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
        # Momentum is normalized over eligible scheduled sessions, so a new habit
        # performing at 90% is not penalized for pre-start calendar days.
        self.assertEqual(metrics["recent_momentum"], 90.0)
        self.assertEqual(metrics["consistency_score"], 69.0)

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

    def test_recent_momentum_treats_perfect_daily_and_weekly_habits_equally(self):
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

        self.assertEqual(daily_metrics["recent_momentum"], 100.0)
        self.assertEqual(weekly_metrics["recent_momentum"], 100.0)
        self.assertEqual(daily_metrics["consistency_score"], 100.0)
        self.assertEqual(weekly_metrics["consistency_score"], 100.0)

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
        self.assertEqual(metrics["recent_momentum"], 100.0)
        self.assertEqual(metrics["consistency_score"], 100.0)

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
        self.assertEqual(metrics["recent_momentum"], 100.0)
        self.assertEqual(metrics["consistency_score"], 100.0)

    def test_recent_momentum_loads_lookback_before_a_short_report_period(self):
        start = date(2026, 1, 1)
        report_start = start + timedelta(days=7)
        report_end = start + timedelta(days=13)
        habit = self.make_habit("Two-week context", start)
        for offset in range(7):
            self.log_completion(habit, start + timedelta(days=offset), 100)

        metrics = habit_performance_metrics(habit, report_start, report_end)
        cached_metrics = habit_performance_metrics(
            habit,
            report_start,
            report_end,
            completion_map={},
            value_map={},
        )

        self.assertEqual(metrics["scheduled_total"], 7)
        self.assertEqual(metrics["completion_rate"], 0.0)
        self.assertEqual(metrics["recent_momentum"], 26.7)
        self.assertEqual(cached_metrics["recent_momentum"], 26.7)

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

        # Perfect performance earns full momentum regardless of schedule cadence.
        # The daily habit has no logged completions, so its score is 0.
        high_weight = 1.3 * sqrt(1)
        low_weight = 0.8 * sqrt(4)
        weekly_score = 100.0
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
        self.assertEqual(metrics["consistency_score"], 42.2)
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
        self.assertEqual(breakdown["current_score"], 97.0)
        self.assertEqual(breakdown["previous_score"], 45.0)
        self.assertEqual(breakdown["score_delta"], 52.0)
        self.assertEqual(components["completion_quality"]["points_delta"], 22.5)
        self.assertEqual(components["full_completion"]["points_delta"], 25.0)
        self.assertEqual(components["rhythm_stability"]["points_delta"], 0.0)
        self.assertEqual(components["recent_momentum"]["points_delta"], 4.5)

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
        self.assertEqual(drivers["improved"]["score_delta"], 93.9)
        self.assertEqual(drivers["declined"]["habit"], declined)
        self.assertEqual(drivers["declined"]["score_delta"], -93.9)

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

        self.client.force_login(self.user)
        with patch("habits.views.timezone.localdate", return_value=today), patch(
            "habits.services.timezone.localdate",
            return_value=today,
        ):
            response = self.client.get(reverse("habits:habit_detail", args=[habit.id]))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["stats"]["average_completion"], 0.0)
        self.assertEqual(response.context["all_time_stats"]["average_completion"], 36.2)
        self.assertEqual(len(response.context["history"]), 20)
        self.assertContains(response, "All time")

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
        self.assertNotIn("daily_recap_dismissed_for", self.client.session)

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
                self.assertNotIn("daily_recap_dismissed_for", self.client.session)

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
        self.assertNotIn("daily_recap_dismissed_for", self.client.session)

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
        self.assertEqual(
            self.client.session["daily_recap_dismissed_for"],
            yesterday.isoformat(),
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
