from calendar import monthrange
from datetime import date, timedelta

from django.utils import timezone

from .models import Habit, HabitCompletion


def iter_scheduled_dates(habit, start_date, end_date):
    if end_date < start_date:
        return []
    if end_date < habit.start_date:
        return []

    start_date = max(start_date, habit.start_date)

    if habit.schedule_type == Habit.SCHEDULE_DAILY:
        current = start_date
        while current <= end_date:
            yield current
            current += timedelta(days=1)
        return

    if habit.schedule_type in (Habit.SCHEDULE_WEEKLY, Habit.SCHEDULE_INTERVAL):
        if habit.schedule_type == Habit.SCHEDULE_WEEKLY:
            interval = max(1, habit.weekly_interval) * 7
        else:
            interval = max(1, habit.interval_days)

        delta_days = (start_date - habit.start_date).days
        if delta_days < 0:
            delta_days = 0
        remainder = delta_days % interval
        if remainder != 0:
            start_date = start_date + timedelta(days=interval - remainder)

        current = start_date
        while current <= end_date:
            yield current
            current += timedelta(days=interval)
        return

    if habit.schedule_type == Habit.SCHEDULE_DAYS:
        days = habit.get_days_of_week_set()
        if not days:
            return []
        current = start_date
        while current <= end_date:
            if current.weekday() in days:
                yield current
            current += timedelta(days=1)
        return

    return []


def get_completion_maps(habit, start_date, end_date):
    completions = HabitCompletion.objects.filter(
        habit=habit,
        date__range=(start_date, end_date),
    )
    completion_map = {}
    value_map = {}
    for completion in completions:
        completion_map[completion.date] = float(completion.completion_percentage or 0)
        if completion.raw_value is not None:
            if habit.habit_type == Habit.HABIT_QUANTITATIVE:
                value_map[completion.date] = int(completion.raw_value)
            else:
                value_map[completion.date] = float(completion.raw_value)
    return completion_map, value_map


def _is_completed(percentage_value):
    return percentage_value >= 100


def _streak_metrics(scheduled_dates, completion_map):
    max_streak = 0
    current_run = 0
    streak_breaks = 0
    completed_total = 0

    for scheduled_date in scheduled_dates:
        if _is_completed(completion_map.get(scheduled_date, 0)):
            completed_total += 1
            current_run += 1
            if current_run > max_streak:
                max_streak = current_run
        else:
            if current_run > 0:
                streak_breaks += 1
            current_run = 0

    current_streak = 0
    for scheduled_date in reversed(scheduled_dates):
        if _is_completed(completion_map.get(scheduled_date, 0)):
            current_streak += 1
        else:
            break

    return {
        "current_streak": current_streak,
        "max_streak": max_streak,
        "streak_breaks": streak_breaks,
        "completed_total": completed_total,
    }


def _consistency_score(completed_ratio):
    return round(completed_ratio * 100, 1)


def habit_performance_metrics(habit, start_date, end_date, completion_map=None, value_map=None):
    scheduled_dates = list(iter_scheduled_dates(habit, start_date, end_date))
    scheduled_total = len(scheduled_dates)

    if completion_map is None or value_map is None:
        completion_map, value_map = get_completion_maps(habit, start_date, end_date)

    if scheduled_total == 0:
        return {
            "scheduled_total": 0,
            "completed_total": 0,
            "missed_total": 0,
            "completion_rate": 0.0,
            "current_streak": 0,
            "max_streak": 0,
            "streak_stability": 0.0,
            "consistency_score": 0.0,
            "average_completion": 0.0,
            "average_value": 0.0,
        }

    streaks = _streak_metrics(scheduled_dates, completion_map)
    completed_total = streaks["completed_total"]
    missed_total = scheduled_total - completed_total
    total_completion = 0.0
    total_value = 0.0
    for scheduled_date in scheduled_dates:
        total_completion += completion_map.get(scheduled_date, 0) or 0
        total_value += value_map.get(scheduled_date, 0) or 0
    average_completion = round(total_completion / scheduled_total, 1)
    completion_rate = average_completion
    if habit.habit_type == Habit.HABIT_QUANTITATIVE:
        average_value = round(total_value / scheduled_total)
    else:
        average_value = round(total_value / scheduled_total, 2)

    if scheduled_total == 1:
        streak_stability = 1.0 if completed_total else 0.0
    else:
        break_ratio = streaks["streak_breaks"] / (scheduled_total - 1)
        streak_stability = max(0.0, round(1 - break_ratio, 4))
    completed_ratio = completed_total / scheduled_total
    consistency_score = _consistency_score(completed_ratio=completed_ratio)

    return {
        "scheduled_total": scheduled_total,
        "completed_total": completed_total,
        "missed_total": missed_total,
        "completion_rate": completion_rate,
        "current_streak": streaks["current_streak"],
        "max_streak": streaks["max_streak"],
        "streak_stability": round(streak_stability * 100, 1),
        "consistency_score": consistency_score,
        "average_completion": average_completion,
        "average_value": average_value,
    }


def calculate_streaks(habit, up_to_date=None):
    if up_to_date is None:
        up_to_date = timezone.localdate()

    metrics = habit_performance_metrics(habit, habit.start_date, up_to_date)
    return metrics["current_streak"], metrics["max_streak"]


def completion_stats(habit, start_date, end_date, completion_map=None, value_map=None):
    metrics = habit_performance_metrics(
        habit,
        start_date,
        end_date,
        completion_map,
        value_map,
    )
    return {
        "scheduled_total": metrics["scheduled_total"],
        "completed_total": metrics["completed_total"],
        "completion_rate": metrics["completion_rate"],
        "average_completion": metrics["average_completion"],
        "average_value": metrics["average_value"],
    }


def calculate_overall_consistency(habits, start_date, end_date):
    weighted_score = 0.0
    weighted_sessions = 0

    for habit in habits:
        metrics = habit_performance_metrics(habit, start_date, end_date)
        if metrics["scheduled_total"] == 0:
            continue
        weighted_sessions += metrics["scheduled_total"]
        weighted_score += metrics["consistency_score"] * metrics["scheduled_total"]

    if weighted_sessions == 0:
        return 0.0
    return round(weighted_score / weighted_sessions, 1)


def compare_habits(habit_a, habit_b, start_date, end_date):
    metrics_a = habit_performance_metrics(habit_a, start_date, end_date)
    metrics_b = habit_performance_metrics(habit_b, start_date, end_date)
    return {
        "habit_a": habit_a,
        "habit_b": habit_b,
        "metrics_a": metrics_a,
        "metrics_b": metrics_b,
        "completion_diff": round(
            metrics_a["completion_rate"] - metrics_b["completion_rate"], 1
        ),
        "current_streak_diff": metrics_a["current_streak"] - metrics_b["current_streak"],
        "max_streak_diff": metrics_a["max_streak"] - metrics_b["max_streak"],
        "consistency_diff": round(
            metrics_a["consistency_score"] - metrics_b["consistency_score"], 1
        ),
    }


def _build_period_snapshot(habits, start_date, end_date, label):
    total_scheduled = 0
    total_completed = 0
    total_completion = 0.0
    tracked_habits = 0
    sum_current_streak = 0
    sum_max_streak = 0
    consistency_weight = 0.0

    for habit in habits:
        metrics = habit_performance_metrics(habit, start_date, end_date)
        if metrics["scheduled_total"] == 0:
            continue
        tracked_habits += 1
        total_scheduled += metrics["scheduled_total"]
        total_completed += metrics["completed_total"]
        total_completion += metrics["completion_rate"] * metrics["scheduled_total"]
        sum_current_streak += metrics["current_streak"]
        sum_max_streak += metrics["max_streak"]
        consistency_weight += metrics["consistency_score"] * metrics["scheduled_total"]

    completion_rate = round(total_completion / total_scheduled, 1) if total_scheduled else 0.0
    average_current_streak = (
        round(sum_current_streak / tracked_habits, 1) if tracked_habits else 0.0
    )
    average_max_streak = round(sum_max_streak / tracked_habits, 1) if tracked_habits else 0.0
    consistency_score = (
        round(consistency_weight / total_scheduled, 1) if total_scheduled else 0.0
    )

    return {
        "label": label,
        "start_date": start_date,
        "end_date": end_date,
        "total_scheduled": total_scheduled,
        "total_completed": total_completed,
        "completion_rate": completion_rate,
        "avg_current_streak": average_current_streak,
        "avg_max_streak": average_max_streak,
        "consistency_score": consistency_score,
    }


def build_weekly_reports(habits, weeks=8, today=None):
    if today is None:
        today = timezone.localdate()

    reports = []
    for week_index in reversed(range(weeks)):
        period_end = today - timedelta(days=week_index * 7)
        period_start = period_end - timedelta(days=6)
        label = f"{period_start.strftime('%b %d')} - {period_end.strftime('%b %d')}"
        reports.append(_build_period_snapshot(habits, period_start, period_end, label))

    _annotate_streak_changes(reports)
    return reports


def _shift_month(base_date, months_back):
    month = base_date.month - months_back
    year = base_date.year
    while month <= 0:
        month += 12
        year -= 1
    return year, month


def build_monthly_reports(habits, months=6, today=None):
    if today is None:
        today = timezone.localdate()

    reports = []
    for months_back in reversed(range(months)):
        year, month = _shift_month(today, months_back)
        first_day = date(year, month, 1)
        last_day = date(year, month, monthrange(year, month)[1])
        if months_back == 0 and today < last_day:
            last_day = today
        reports.append(
            _build_period_snapshot(
                habits,
                first_day,
                last_day,
                first_day.strftime("%b %Y"),
            )
        )

    _annotate_streak_changes(reports)
    return reports


def _annotate_streak_changes(reports):
    previous = None
    for report in reports:
        if previous is None:
            report["streak_change"] = 0.0
        else:
            report["streak_change"] = round(
                report["avg_current_streak"] - previous["avg_current_streak"], 1
            )
        previous = report


def get_next_scheduled_date(habit, from_date=None):
    if from_date is None:
        from_date = timezone.localdate()

    if from_date < habit.start_date:
        from_date = habit.start_date

    if habit.schedule_type == Habit.SCHEDULE_DAILY:
        return from_date

    if habit.schedule_type == Habit.SCHEDULE_WEEKLY:
        interval = max(1, habit.weekly_interval) * 7
        delta_days = (from_date - habit.start_date).days
        remainder = delta_days % interval
        if remainder == 0:
            return from_date
        return from_date + timedelta(days=interval - remainder)

    if habit.schedule_type == Habit.SCHEDULE_INTERVAL:
        interval = max(1, habit.interval_days)
        delta_days = (from_date - habit.start_date).days
        remainder = delta_days % interval
        if remainder == 0:
            return from_date
        return from_date + timedelta(days=interval - remainder)

    if habit.schedule_type == Habit.SCHEDULE_DAYS:
        days = habit.get_days_of_week_set()
        if not days:
            return None
        for offset in range(0, 7):
            candidate = from_date + timedelta(days=offset)
            if candidate.weekday() in days:
                return candidate
        return None

    return None
