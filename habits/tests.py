from datetime import date, timedelta
from decimal import Decimal
from math import sqrt
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from .models import (
    DEFAULT_CATEGORIES,
    FriendRequest,
    Habit,
    HabitCategory,
    HabitCompletion,
)
from .services import (
    build_category_analytics,
    build_habit_score_drivers,
    build_overall_score_breakdown,
    calculate_overall_consistency,
    habit_performance_metrics,
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
            categories = [self.categories["personal"]]
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
        self.assertEqual(breakdown["current_score"], 100.0)
        self.assertEqual(breakdown["previous_score"], 45.0)
        self.assertEqual(breakdown["score_delta"], 55.0)
        self.assertEqual(components["completion_quality"]["points_delta"], 22.5)
        self.assertEqual(components["full_completion"]["points_delta"], 25.0)
        self.assertEqual(components["rhythm_stability"]["points_delta"], 0.0)
        self.assertEqual(components["recent_momentum"]["points_delta"], 7.5)

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
        self.assertEqual(drivers["improved"]["score_delta"], 100.0)
        self.assertEqual(drivers["declined"]["habit"], declined)
        self.assertEqual(drivers["declined"]["score_delta"], -100.0)

    def test_category_analytics_marks_best_and_weakest_categories(self):
        start = date(2026, 1, 1)
        health = self.make_habit("Lift", start, categories=[self.categories["health"]])
        study = self.make_habit("Read", start, categories=[self.categories["study"]])
        work = self.make_habit("Ship", start, categories=[self.categories["work"]])

        for offset in range(2):
            self.log_completion(health, start + timedelta(days=offset), 100)
            self.log_completion(study, start + timedelta(days=offset), 50)

        analytics = build_category_analytics(
            [health, study, work],
            start,
            start + timedelta(days=1),
        )
        summaries = {item["key"]: item for item in analytics["summaries"]}

        self.assertEqual(len(analytics["summaries"]), len(DEFAULT_CATEGORIES))
        self.assertEqual(summaries["health"]["completion_rate"], 100.0)
        self.assertEqual(summaries["study"]["completion_rate"], 50.0)
        self.assertEqual(summaries["work"]["completion_rate"], 0.0)
        self.assertEqual(analytics["best"]["key"], "health")
        self.assertEqual(analytics["weakest"]["key"], "work")

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
        self.assertContains(response, "alice")
        self.assertContains(response, "bob")
        self.assertNotContains(response, "cara")

        content = response.content.decode()
        ranking_markup = content.split('<div class="leaderboard-list">', 1)[1]
        self.assertLess(ranking_markup.index("bob"), ranking_markup.index("alice"))
