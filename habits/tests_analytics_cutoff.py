"""Regression cover for the global finalized-analytics cutoff.

Analytics use finalized activity through yesterday; today's activity is
included after the local day ends. The Today page is the single exception and
must keep updating live.

Every test here pins ``timezone.localdate`` (or ``timezone.now`` plus a
``TIME_ZONE``) so the cutoff is deterministic and the local-timezone behaviour
is actually exercised rather than assumed.
"""

import json
from datetime import date, datetime, timedelta, timezone as dt_timezone
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils.html import escape

from .models import FriendRequest, Habit, HabitCategory, HabitCompletion
from .services import (
    ANALYTICS_CUTOFF_NOTE,
    analytics_end_date,
    analytics_window,
    build_category_analytics,
    build_overall_score_breakdown,
    calculate_streaks,
    clamp_analytics_end,
    completion_stats,
    compute_today_metrics,
    compute_user_metrics,
    daily_average_completion_series,
    habit_performance_metrics,
    habit_tracking_start,
    iter_finalized_occurrences,
    iter_scheduled_occurrences,
    leaderboard_ranking_score,
    RHYTHM_SESSION_WINDOW,
    SCORE_EVIDENCE_FULL_SESSIONS,
    _rhythm_participation,
    _score_evidence,
)


TODAY = date(2026, 8, 10)


class AnalyticsCutoffTestMixin:
    """Shared fixture: a daily habit with six finalized 100% sessions."""

    today = TODAY

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="cutoff-user",
            password="not-used",
        )
        # Six finalized days (Aug 4 - Aug 9) plus today (Aug 10).
        self.start = self.today - timedelta(days=6)
        self.habit = self._make_habit("Daily reading")
        for offset in range(6):
            self._log(self.habit, self.start + timedelta(days=offset), 100)

    def _make_habit(self, name, priority=Habit.PRIORITY_MEDIUM, user=None):
        return Habit.objects.create(
            user=user or self.user,
            name=name,
            habit_type=Habit.HABIT_PARTIAL,
            schedule_type=Habit.SCHEDULE_DAILY,
            priority=priority,
            start_date=self.start,
        )

    def _log(self, habit, target_date, percentage):
        completion, _ = HabitCompletion.objects.get_or_create(
            habit=habit,
            date=target_date,
        )
        completion.completion_percentage = Decimal(str(percentage))
        completion.raw_value = Decimal(str(percentage))
        completion.save(update_fields=["completion_percentage", "raw_value"])
        return completion

    def _freeze_services(self):
        return patch(
            "habits.services.timezone.localdate",
            return_value=self.today,
        )

    def _freeze_request(self):
        """Freeze both view- and service-level clocks for a request."""
        return (
            patch("habits.views.timezone.localdate", return_value=self.today),
            patch("habits.services.timezone.localdate", return_value=self.today),
        )


class AnalyticsWindowHelperTests(TestCase):
    """The cutoff and window helpers are the single source of truth."""

    def test_cutoff_is_the_day_before_the_local_today(self):
        self.assertEqual(analytics_end_date(TODAY), date(2026, 8, 9))

    def test_clamp_trims_today_and_the_future_but_leaves_the_past_alone(self):
        self.assertEqual(clamp_analytics_end(TODAY, TODAY), date(2026, 8, 9))
        self.assertEqual(
            clamp_analytics_end(TODAY + timedelta(days=5), TODAY),
            date(2026, 8, 9),
        )
        self.assertEqual(
            clamp_analytics_end(date(2026, 7, 1), TODAY),
            date(2026, 7, 1),
        )

    def test_periods_end_yesterday_and_keep_their_full_length(self):
        # With today = Aug 10, a "30-day" window must be 30 finalized days
        # (Jul 11 - Aug 9), not a 29-day window created by trimming only the
        # end of a today-anchored range.
        for days, expected_start in (
            (14, date(2026, 7, 27)),
            (15, date(2026, 7, 26)),
            (30, date(2026, 7, 11)),
            (90, date(2026, 5, 12)),
        ):
            with self.subTest(days=days):
                start, end = analytics_window(days, TODAY)
                self.assertEqual(end, date(2026, 8, 9))
                self.assertEqual(start, expected_start)
                self.assertEqual((end - start).days + 1, days)

    def test_helpers_default_to_the_local_today(self):
        with patch("habits.services.timezone.localdate", return_value=TODAY):
            self.assertEqual(analytics_end_date(), date(2026, 8, 9))
            self.assertEqual(analytics_window(30), (date(2026, 7, 11), date(2026, 8, 9)))


class AnalyticsCutoffTimezoneTests(TestCase):
    """The cutoff follows the user's local day, not UTC, with no cron job."""

    def _at_utc_instant(self, moment):
        return patch("django.utils.timezone.now", return_value=moment)

    def test_cutoff_uses_the_local_date_rather_than_the_utc_date(self):
        # 23:30 UTC on Aug 10 is already Aug 11 in Dhaka (UTC+6), so the two
        # zones must disagree about which day is finalized.
        moment = datetime(2026, 8, 10, 23, 30, tzinfo=dt_timezone.utc)

        with self._at_utc_instant(moment):
            with override_settings(TIME_ZONE="UTC"):
                self.assertEqual(analytics_end_date(), date(2026, 8, 9))
            with override_settings(TIME_ZONE="Asia/Dhaka"):
                self.assertEqual(analytics_end_date(), date(2026, 8, 10))

    @override_settings(TIME_ZONE="Asia/Dhaka")
    def test_cutoff_advances_on_its_own_at_local_midnight(self):
        # 17:59 UTC = 23:59 local (day not finished), 18:01 UTC = 00:01 local
        # the next day. Nothing runs in between: the cutoff is derived per
        # call, so no midnight cron job is required for correctness.
        before_midnight = datetime(2026, 8, 10, 17, 59, tzinfo=dt_timezone.utc)
        after_midnight = datetime(2026, 8, 10, 18, 1, tzinfo=dt_timezone.utc)

        with self._at_utc_instant(before_midnight):
            self.assertEqual(analytics_end_date(), date(2026, 8, 9))
        with self._at_utc_instant(after_midnight):
            self.assertEqual(analytics_end_date(), date(2026, 8, 10))


class TodaySessionExclusionTests(AnalyticsCutoffTestMixin, TestCase):
    """Untouched, partial, and completed sessions today must not move anything."""

    def _metrics(self):
        with self._freeze_services():
            return habit_performance_metrics(self.habit, self.start, self.today)

    def test_todays_progress_never_changes_habit_analytics(self):
        baseline = self._metrics()

        # Six finalized 100% sessions: analytics must keep seeing exactly
        # 100, 100, 100, 100, 100, 100 no matter what today reads.
        self.assertEqual(baseline["scheduled_total"], 6)
        self.assertEqual(baseline["completed_total"], 6)
        self.assertEqual(baseline["completion_rate"], 100.0)

        for todays_progress in (0, 40, 100):
            with self.subTest(todays_progress=todays_progress):
                self._log(self.habit, self.today, todays_progress)
                self.assertEqual(self._metrics(), baseline)

    def test_untouched_today_does_not_add_a_scheduled_session(self):
        # The session exists on the schedule, but an unfinished day must not
        # be counted as evidence — otherwise a perfect record would read 6/7.
        metrics = self._metrics()
        self.assertEqual(metrics["scheduled_total"], 6)
        self.assertEqual(metrics["missed_total"], 0)

    def test_qfrme_components_all_exclude_today(self):
        baseline = self._metrics()
        component_keys = (
            "completion_quality",
            "full_completion_reliability",
            "streak_stability",
            "recent_momentum",
            "score_evidence",
            "consistency_score",
        )

        # A 0% day would drag Q, F, R, and M down; a 100% day would add
        # evidence. Neither may happen while the day is unfinished.
        for todays_progress in (0, 40, 100):
            self._log(self.habit, self.today, todays_progress)
            current = self._metrics()
            for key in component_keys:
                with self.subTest(todays_progress=todays_progress, component=key):
                    self.assertEqual(current[key], baseline[key])

    def test_evidence_counts_only_finalized_sessions(self):
        self._log(self.habit, self.today, 100)
        metrics = self._metrics()
        self.assertEqual(metrics["score_evidence"], _score_evidence(6))
        self.assertNotEqual(metrics["score_evidence"], _score_evidence(7))

    def test_rhythm_and_momentum_windows_never_include_today(self):
        self._log(self.habit, self.today, 0)
        with self._freeze_services():
            occurrences = list(
                iter_finalized_occurrences(self.habit, self.start, self.today)
            )

        dates = [occurrence.date for occurrence in occurrences]
        self.assertNotIn(self.today, dates)
        self.assertEqual(max(dates), analytics_end_date(self.today))
        # The last 7 (Rhythm) and last 6 (Momentum) sessions are taken from
        # this list, so both windows inherit the cutoff.
        self.assertNotIn(self.today, dates[-RHYTHM_SESSION_WINDOW:])
        self.assertNotIn(self.today, dates[-6:])

    def test_only_the_analytics_gate_trims_not_the_scheduling_primitive(self):
        # ``iter_finalized_occurrences`` is the shared chokepoint: every
        # historical metric counts sessions through it, which is why no metric
        # needs its own patch. The raw scheduling primitive underneath must
        # stay untrimmed, because the Today page and the schedule preview
        # legitimately need to see today and future dates.
        far_end = self.today + timedelta(days=30)

        with self._freeze_services():
            raw = list(iter_scheduled_occurrences(self.habit, self.start, far_end))
            finalized = list(
                iter_finalized_occurrences(self.habit, self.start, far_end)
            )

        self.assertEqual(max(item.date for item in raw), far_end)
        self.assertEqual(max(item.date for item in finalized), date(2026, 8, 9))

    def test_streaks_and_completion_stats_exclude_today(self):
        with self._freeze_services():
            baseline_streaks = calculate_streaks(self.habit, self.today)
            baseline_stats = completion_stats(self.habit, self.start, self.today)

        self._log(self.habit, self.today, 0)
        with self._freeze_services():
            self.assertEqual(calculate_streaks(self.habit, self.today), baseline_streaks)
            self.assertEqual(
                completion_stats(self.habit, self.start, self.today),
                baseline_stats,
            )

    def test_todays_session_becomes_eligible_once_it_is_yesterday(self):
        self._log(self.habit, self.today, 40)

        with self._freeze_services():
            before = habit_performance_metrics(self.habit, self.start, self.today)
        self.assertEqual(before["scheduled_total"], 6)
        self.assertEqual(before["completion_rate"], 100.0)

        tomorrow = self.today + timedelta(days=1)
        with patch("habits.services.timezone.localdate", return_value=tomorrow):
            after = habit_performance_metrics(self.habit, self.start, tomorrow)

        # The same stored 40% row now counts, because the day has ended.
        self.assertEqual(after["scheduled_total"], 7)
        self.assertLess(after["completion_rate"], 100.0)
        self.assertEqual(after["completion_rate"], round(640 / 7, 1))


class AggregateAnalyticsCutoffTests(AnalyticsCutoffTestMixin, TestCase):
    """Cross-habit aggregation, charts, and category analytics exclude today."""

    def setUp(self):
        super().setUp()
        self.second_habit = self._make_habit("Evening walk", Habit.PRIORITY_HIGH)
        for offset in range(6):
            self._log(self.second_habit, self.start + timedelta(days=offset), 100)
        self.habits = [self.habit, self.second_habit]

    def test_completion_rate_and_overall_aggregation_exclude_today(self):
        with self._freeze_services():
            baseline = compute_user_metrics(self.habits, self.start, self.today)

        self.assertEqual(baseline["aggregate"]["total_scheduled"], 12)
        self.assertEqual(baseline["aggregate"]["completion_rate"], 100.0)

        for todays_progress in (0, 40, 100):
            with self.subTest(todays_progress=todays_progress):
                for habit in self.habits:
                    self._log(habit, self.today, todays_progress)
                with self._freeze_services():
                    current = compute_user_metrics(self.habits, self.start, self.today)
                self.assertEqual(
                    current["aggregate"],
                    baseline["aggregate"],
                )
                self.assertEqual(
                    current["consistency_score"],
                    baseline["consistency_score"],
                )

    def test_score_breakdown_and_drivers_exclude_today(self):
        window_start, window_end = analytics_window(30, self.today)
        previous_end = window_start - timedelta(days=1)
        previous_start = previous_end - timedelta(days=29)

        with self._freeze_services():
            baseline = build_overall_score_breakdown(
                self.habits,
                window_start,
                window_end,
                previous_start,
                previous_end,
            )

        for habit in self.habits:
            self._log(habit, self.today, 0)

        with self._freeze_services():
            current = build_overall_score_breakdown(
                self.habits,
                window_start,
                window_end,
                previous_start,
                previous_end,
            )

        self.assertEqual(current["current_score"], baseline["current_score"])
        self.assertEqual(
            [component["current_points"] for component in current["components"]],
            [component["current_points"] for component in baseline["components"]],
        )

    def test_chart_series_stops_at_yesterday_and_never_labels_today(self):
        self._log(self.habit, self.today, 100)
        chart_start, chart_end = analytics_window(14, self.today)

        with self._freeze_services():
            series = daily_average_completion_series(
                self.habits,
                chart_start,
                chart_end,
            )

        dates = [point["date"] for point in series]
        labels = [point["label"] for point in series]
        self.assertEqual(max(dates), date(2026, 8, 9))
        self.assertNotIn(self.today, dates)
        self.assertNotIn(self.today.strftime("%b %d"), labels)

    def test_chart_series_ignores_a_today_end_date_passed_by_a_caller(self):
        with self._freeze_services():
            series = daily_average_completion_series(
                self.habits,
                self.start,
                self.today,
            )
        self.assertEqual(max(point["date"] for point in series), date(2026, 8, 9))

    def test_category_analytics_exclude_today(self):
        category = HabitCategory.objects.first()
        self.assertIsNotNone(category)
        self.habit.categories.add(category)

        with self._freeze_services():
            baseline = build_category_analytics(self.habits, self.start, self.today)

        self._log(self.habit, self.today, 0)
        with self._freeze_services():
            current = build_category_analytics(self.habits, self.start, self.today)

        self.assertEqual(current, baseline)

    def test_category_evidence_counts_exclude_today(self):
        category = HabitCategory.objects.first()
        self.habit.categories.add(category)

        self._log(self.habit, self.today, 100)
        with self._freeze_services():
            analytics = build_category_analytics(self.habits, self.start, self.today)

        summary = next(
            item for item in analytics["summaries"] if item["key"] == category.key
        )
        self.assertEqual(summary["scheduled_total"], 6)


class TodayPageLiveBehaviourTests(AnalyticsCutoffTestMixin, TestCase):
    """The Today page is the exception: it must keep updating immediately."""

    def test_today_metrics_reflect_progress_as_it_is_logged(self):
        with self._freeze_services():
            untouched = compute_today_metrics(self.user, self.today)
        self.assertEqual(untouched["scheduled_count"], 1)
        self.assertEqual(untouched["completed_count"], 0)
        self.assertEqual(untouched["completion_rate"], 0.0)

        self._log(self.habit, self.today, 40)
        with self._freeze_services():
            partial = compute_today_metrics(self.user, self.today)
        self.assertEqual(partial["completion_rate"], 40.0)
        self.assertEqual(partial["completed_count"], 0)

        self._log(self.habit, self.today, 100)
        with self._freeze_services():
            finished = compute_today_metrics(self.user, self.today)
        self.assertEqual(finished["completion_rate"], 100.0)
        self.assertEqual(finished["completed_count"], 1)

    def test_today_page_shows_live_progress_while_analytics_stay_finalized(self):
        self.client.force_login(self.user)
        view_clock, service_clock = self._freeze_request()

        with view_clock, service_clock:
            response = self.client.post(
                reverse("habits:update_progress", args=[self.habit.id]),
                {
                    "date": self.today.isoformat(),
                    "completion_percentage": "100",
                },
                HTTP_X_REQUESTED_WITH="XMLHttpRequest",
            )
            payload = json.loads(response.content)
            dashboard = self.client.get(reverse("habits:dashboard"))

        # Live today UI updated...
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["completion_rate"], 100.0)
        self.assertEqual(payload["completed_count"], 1)
        # ...while the dashboard still counts only the six finalized sessions.
        self.assertEqual(dashboard.context["total_scheduled"], 6)
        self.assertEqual(dashboard.context["total_completed"], 6)

    def test_progress_logged_today_still_counts_tomorrow(self):
        self._log(self.habit, self.today, 100)
        tomorrow = self.today + timedelta(days=1)

        with patch("habits.services.timezone.localdate", return_value=tomorrow):
            metrics = habit_performance_metrics(self.habit, self.start, tomorrow)

        self.assertEqual(metrics["scheduled_total"], 7)
        self.assertEqual(metrics["completed_total"], 7)


class AnalyticsViewCutoffTests(AnalyticsCutoffTestMixin, TestCase):
    """Rendered analytics pages inherit the cutoff and explain it."""

    def setUp(self):
        super().setUp()
        self.client.force_login(self.user)

    def _get(self, url_name, *args, **kwargs):
        view_clock, service_clock = self._freeze_request()
        with view_clock, service_clock:
            return self.client.get(reverse(url_name, args=args), kwargs)

    def test_dashboard_window_ends_yesterday_and_keeps_thirty_days(self):
        response = self._get("habits:dashboard")

        self.assertEqual(response.context["analytics_end_date"], date(2026, 8, 9))
        self.assertEqual(response.context["dashboard_window_label"], "Jul 11 - Aug 09")
        self.assertEqual(response.context["total_scheduled"], 6)

    def test_dashboard_chart_labels_never_include_today(self):
        self._log(self.habit, self.today, 100)
        response = self._get("habits:dashboard")

        labels = json.loads(response.context["chart_labels"])
        self.assertNotIn(self.today.strftime("%b %d"), labels)
        self.assertEqual(labels[-1], "Aug 09")

    def test_dashboard_explains_the_cutoff(self):
        response = self._get("habits:dashboard")
        self.assertContains(response, escape(ANALYTICS_CUTOFF_NOTE))

    def test_habit_detail_chart_excludes_today_but_history_stays_live(self):
        self._log(self.habit, self.today, 40)
        response = self._get("habits:habit_detail", self.habit.id)

        labels = json.loads(response.context["chart_labels"])
        self.assertNotIn(self.today.strftime("%b %d"), labels)
        self.assertEqual(labels[-1], "Aug 09")
        # The history list still shows today as a live, pending row.
        self.assertEqual(response.context["history"][-1]["date"], self.today)
        self.assertEqual(response.context["history"][-1]["status"], "pending")

    def test_habit_compare_windows_end_yesterday(self):
        other = self._make_habit("Stretching")
        response = self._get(
            "habits:habit_compare",
            **{"habit_ids": [self.habit.id, other.id]},
        )

        self.assertEqual(response.context["window_start"], date(2026, 5, 12))
        payload = json.loads(response.context["chart_payload"])
        self.assertNotIn(self.today.strftime("%b %d"), payload["last30"]["labels"])
        self.assertEqual(payload["last30"]["labels"][-1], "Aug 09")

    def test_reports_and_profile_explain_the_cutoff(self):
        for url_name, args in (
            ("habits:reports", ()),
            ("habits:user_profile", (self.user.username,)),
            ("habits:habit_compare", ()),
        ):
            with self.subTest(url_name=url_name):
                response = self._get(url_name, *args)
                self.assertContains(response, escape(ANALYTICS_CUTOFF_NOTE))

    def test_profile_daily_chart_ends_yesterday(self):
        self._log(self.habit, self.today, 100)
        response = self._get("habits:user_profile", self.user.username)

        labels = json.loads(response.context["daily_labels"])
        self.assertNotIn(self.today.strftime("%b %d"), labels)
        self.assertEqual(labels[-1], "Aug 09")


class LeaderboardCutoffTests(AnalyticsCutoffTestMixin, TestCase):
    """Ranking, evidence counts, and window labels all stop at yesterday."""

    def setUp(self):
        super().setUp()
        self.friend = get_user_model().objects.create_user(
            username="cutoff-friend",
            password="not-used",
        )
        FriendRequest.objects.create(
            from_user=self.user,
            to_user=self.friend,
            status=FriendRequest.STATUS_ACCEPTED,
        )
        self.friend_habit = self._make_habit("Friend habit", user=self.friend)
        for offset in range(6):
            self._log(self.friend_habit, self.start + timedelta(days=offset), 0)
        self.client.force_login(self.user)

    def _leaderboard(self, window="current"):
        view_clock, service_clock = self._freeze_request()
        with view_clock, service_clock:
            return self.client.get(reverse("habits:leaderboard"), {"window": window})

    def _entry_for(self, response, username):
        return next(
            entry
            for entry in response.context["leaderboard_entries"]
            if entry["user"].username == username
        )

    def test_todays_perfect_day_cannot_change_scores_or_ranking(self):
        baseline = self._leaderboard()
        baseline_order = [
            entry["user"].username for entry in baseline.context["leaderboard_entries"]
        ]
        baseline_friend = self._entry_for(baseline, self.friend.username)

        # The trailing friend finishes today perfectly; ranking must not move
        # until that day is finalized.
        self._log(self.friend_habit, self.today, 100)
        current = self._leaderboard()
        current_order = [
            entry["user"].username for entry in current.context["leaderboard_entries"]
        ]
        current_friend = self._entry_for(current, self.friend.username)

        self.assertEqual(current_order, baseline_order)
        self.assertEqual(
            current_friend["consistency_score"],
            baseline_friend["consistency_score"],
        )
        self.assertEqual(
            current_friend["ranking_score"],
            baseline_friend["ranking_score"],
        )

    def test_evidence_counts_exclude_today(self):
        self._log(self.habit, self.today, 100)
        response = self._leaderboard()
        entry = self._entry_for(response, self.user.username)

        self.assertEqual(entry["total_scheduled"], 6)
        self.assertEqual(
            entry["ranking_score"],
            leaderboard_ranking_score(entry["consistency_score"], 6),
        )

    def test_current_window_is_thirty_finalized_days(self):
        response = self._leaderboard()

        self.assertEqual(response.context["leaderboard_window_start"], date(2026, 7, 11))
        self.assertEqual(response.context["leaderboard_window_end"], date(2026, 8, 9))
        self.assertContains(response, "Jul 11 - Aug 09")

    def test_all_time_window_also_ends_yesterday(self):
        response = self._leaderboard(window="all")

        self.assertEqual(response.context["leaderboard_window_end"], date(2026, 8, 9))
        self.assertContains(response, "Aug 09, 2026")
        self.assertNotContains(response, "Aug 10, 2026")

    def test_leaderboard_explains_the_cutoff(self):
        self.assertContains(self._leaderboard(), escape(ANALYTICS_CUTOFF_NOTE))


class ScoringFormulaUnchangedTests(AnalyticsCutoffTestMixin, TestCase):
    """The cutoff must not have altered the scoring or ranking formulas."""

    def test_rhythm_participation_band_still_ramps_between_45_and_55(self):
        self.assertEqual(_rhythm_participation(45), 0.0)
        self.assertEqual(_rhythm_participation(44), 0.0)
        self.assertAlmostEqual(_rhythm_participation(50), 0.5, places=6)
        self.assertEqual(_rhythm_participation(55), 1.0)
        self.assertEqual(_rhythm_participation(56), 1.0)

    def test_evidence_curve_still_saturates_at_the_rhythm_window(self):
        self.assertEqual(_score_evidence(SCORE_EVIDENCE_FULL_SESSIONS), 1.0)
        self.assertEqual(_score_evidence(0), 0.0)
        self.assertAlmostEqual(_score_evidence(1), 0.56, places=2)
        self.assertAlmostEqual(_score_evidence(3), 0.84, places=2)

    def test_finalized_history_scores_exactly_as_it_did_before(self):
        # Seven finalized perfect sessions still score the documented 92.5,
        # so enforcing the cutoff changed only *which* days are counted.
        self._log(self.habit, self.today, 100)

        tomorrow = self.today + timedelta(days=1)
        with patch("habits.services.timezone.localdate", return_value=tomorrow):
            metrics = habit_performance_metrics(self.habit, self.start, tomorrow)

        self.assertEqual(metrics["scheduled_total"], 7)
        self.assertEqual(metrics["score_evidence"], 1.0)
        self.assertAlmostEqual(metrics["consistency_score"], 92.5, places=1)

    def test_all_time_metrics_start_at_tracking_start_and_end_yesterday(self):
        self._log(self.habit, self.today, 100)
        with self._freeze_services():
            metrics = habit_performance_metrics(
                self.habit,
                habit_tracking_start(self.habit),
                self.today,
            )
        self.assertEqual(metrics["scheduled_total"], 6)
