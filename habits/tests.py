from datetime import date, timedelta
from decimal import Decimal
from math import sqrt
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase

from .models import Habit, HabitCompletion
from .services import calculate_overall_consistency, habit_performance_metrics


class ConsistencyScoreTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="score-user",
            password="not-used",
        )

    def make_habit(
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

        high_weight = 1.3 * sqrt(1)
        low_weight = 0.8 * sqrt(4)
        expected_score = round((100 * high_weight) / (high_weight + low_weight), 1)
        self.assertEqual(overall_score, expected_score)

    def test_unlogged_today_does_not_count_as_missed_until_logged(self):
        today = date(2026, 1, 2)
        yesterday = today - timedelta(days=1)
        habit = self.make_habit("Daily", yesterday)
        self.log_completion(habit, yesterday, 100)

        with patch("habits.services.timezone.localdate", return_value=today):
            metrics = habit_performance_metrics(habit, yesterday, today)

        self.assertEqual(metrics["scheduled_total"], 1)
        self.assertEqual(metrics["completed_total"], 1)
        self.assertEqual(metrics["current_streak"], 1)
        self.assertEqual(metrics["consistency_score"], 100.0)

        self.log_completion(habit, today, 0)
        with patch("habits.services.timezone.localdate", return_value=today):
            metrics = habit_performance_metrics(habit, yesterday, today)

        self.assertEqual(metrics["scheduled_total"], 2)
        self.assertEqual(metrics["completed_total"], 1)
        self.assertEqual(metrics["current_streak"], 0)
        self.assertLess(metrics["consistency_score"], 100.0)
