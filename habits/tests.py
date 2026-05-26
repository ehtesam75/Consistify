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
        self.assertEqual(response.context["all_time_stats"]["average_completion"], 37.0)
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
