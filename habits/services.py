from calendar import monthrange
from datetime import date, timedelta
from math import sqrt

from django.db.models import Q
from django.utils import timezone

from .models import Habit, HabitCategory, HabitCompletion
CONSISTENCY_COMPLETION_QUALITY_WEIGHT = 0.45
CONSISTENCY_FULL_COMPLETION_WEIGHT = 0.25
CONSISTENCY_STREAK_STABILITY_WEIGHT = 0.15
CONSISTENCY_RECENT_MOMENTUM_WEIGHT = 0.15
RECENT_MOMENTUM_WINDOW = 14
PRIORITY_SCORE_WEIGHTS = {
    Habit.PRIORITY_HIGH: 1.3,
    Habit.PRIORITY_MEDIUM: 1.0,
    Habit.PRIORITY_LOW: 0.8,
}
CONSISTENCY_SCORE_COMPONENTS = (
    {
        "key": "completion_quality",
        "label": "Completion quality",
        "metric_key": "completion_quality",
        "weight": CONSISTENCY_COMPLETION_QUALITY_WEIGHT,
        "description": "Average progress across every scheduled session.",
    },
    {
        "key": "full_completion",
        "label": "Full completion",
        "metric_key": "full_completion_reliability",
        "weight": CONSISTENCY_FULL_COMPLETION_WEIGHT,
        "description": "How often scheduled sessions reached 100%.",
    },
    {
        "key": "rhythm_stability",
        "label": "Rhythm stability",
        "metric_key": "streak_stability",
        "weight": CONSISTENCY_STREAK_STABILITY_WEIGHT,
        "description": "How steadily progress continued after momentum started.",
    },
    {
        "key": "recent_momentum",
        "label": "Recent momentum",
        "metric_key": "recent_momentum",
        "weight": CONSISTENCY_RECENT_MOMENTUM_WEIGHT,
        "description": "Recent scheduled sessions, with newer days weighted more.",
    },
)


def should_prompt_daily_recap(previous_login, today=None):
    if today is None:
        today = timezone.localdate()
    if previous_login is None:
        return True
    return timezone.localdate(previous_login) < today


def iter_scheduled_dates(habit, start_date, end_date):
    if end_date < start_date:
        return []
    if end_date < habit.start_date:
        return []

    start_date = max(start_date, habit.start_date)
    pause_ranges = _pause_ranges_for_habit(habit, start_date, end_date)

    if habit.schedule_type == Habit.SCHEDULE_DAILY:
        current = start_date
        while current <= end_date:
            if not _is_paused_on_date(current, pause_ranges):
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
            if not _is_paused_on_date(current, pause_ranges):
                yield current
            current += timedelta(days=interval)
        return

    if habit.schedule_type == Habit.SCHEDULE_DAYS:
        days = habit.get_days_of_week_set()
        if not days:
            return []
        current = start_date
        while current <= end_date:
            if current.weekday() in days and not _is_paused_on_date(current, pause_ranges):
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


def _pause_ranges_for_habit(habit, start_date, end_date):
    if end_date < start_date:
        return []
    cache = getattr(habit, "_pause_ranges_cache", None)
    cache_key = (start_date, end_date)
    if cache is not None and cache_key in cache:
        return cache[cache_key]

    prefetched = getattr(habit, "_prefetched_objects_cache", None)
    pause_ranges = None
    if prefetched and "pauses" in prefetched:
        pause_ranges = [
            (pause.start_date, pause.end_date)
            for pause in prefetched["pauses"]
            if pause.start_date <= end_date
            and (pause.end_date is None or pause.end_date > start_date)
        ]
    else:
        pauses = habit.pauses.filter(start_date__lte=end_date).filter(
            Q(end_date__isnull=True) | Q(end_date__gt=start_date)
        )
        pause_ranges = list(pauses.values_list("start_date", "end_date"))

    if cache is None:
        habit._pause_ranges_cache = {cache_key: pause_ranges}
    else:
        cache[cache_key] = pause_ranges

    return pause_ranges


def _is_paused_on_date(target_date, pause_ranges):
    for start_date, end_date in pause_ranges:
        if start_date <= target_date and (end_date is None or target_date < end_date):
            return True
    return False


def _is_completed(percentage_value):
    return percentage_value >= 100


PRIORITY_WEIGHTS = {
    "low": 1,
    "medium": 2,
    "high": 3,
}


def _priority_weight(habit):
    """Difficulty weight derived from a habit's priority level."""
    return PRIORITY_WEIGHTS.get(getattr(habit, "priority", ""), 1)


def weighted_completion_rate(scheduled_habits, completion_lookup=None):
    """Compute a priority-weighted, partial-aware completion rate (0-100).

    Each scheduled habit contributes its per-habit ``completion_percentage``
    (already 0-100 across binary/partial/quantitative types) scaled by the
    habit's priority weight so harder habits count proportionally more.
    """
    completion_lookup = completion_lookup or {}
    weighted_total = 0.0
    weight_sum = 0.0
    for habit in scheduled_habits:
        weight = _priority_weight(habit)
        weight_sum += weight
        percent = completion_lookup.get(habit.id, 0.0) or 0.0
        try:
            percent = float(percent)
        except (TypeError, ValueError):
            percent = 0.0
        weighted_total += weight * max(0.0, min(100.0, percent))
    if weight_sum <= 0:
        return 0
    return round((weighted_total / (weight_sum * 100)) * 100)


def _clamped_percentage(percentage_value):
    return max(0.0, min(100.0, float(percentage_value or 0)))


def _has_meaningful_progress(percentage_value):
    return _clamped_percentage(percentage_value) > 0


def _scored_scheduled_dates(scheduled_dates, completion_map, end_date):
    if not scheduled_dates:
        return scheduled_dates

    today = timezone.localdate()
    latest_scheduled_date = scheduled_dates[-1]
    if end_date == today and latest_scheduled_date == today and today not in completion_map:
        return scheduled_dates[:-1]
    return scheduled_dates


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


def _rhythm_stability(scheduled_dates, completion_map):
    effort_flags = [
        _has_meaningful_progress(completion_map.get(scheduled_date, 0))
        for scheduled_date in scheduled_dates
    ]
    if not any(effort_flags):
        return 0.0

    first_effort_index = effort_flags.index(True)
    scoped_flags = effort_flags[first_effort_index:]
    if len(scoped_flags) == 1:
        return 1.0

    break_count = sum(
        1
        for previous, current in zip(scoped_flags, scoped_flags[1:])
        if previous and not current
    )
    break_ratio = break_count / (len(scoped_flags) - 1)
    return max(0.0, round(1 - break_ratio, 4))


def _recent_momentum(scheduled_dates, completion_map):
    recent_dates = scheduled_dates[-RECENT_MOMENTUM_WINDOW:]
    if not recent_dates:
        return 0.0

    weighted_completion = 0.0
    total_weight = 0
    for index, scheduled_date in enumerate(recent_dates, start=1):
        weight = index
        weighted_completion += (
            _clamped_percentage(completion_map.get(scheduled_date, 0)) / 100
        ) * weight
        total_weight += weight

    if total_weight == 0:
        return 0.0
    return weighted_completion / total_weight


def _consistency_score(
    completion_quality,
    full_completion_ratio,
    streak_stability,
    recent_momentum,
):
    score = (
        completion_quality * CONSISTENCY_COMPLETION_QUALITY_WEIGHT
        + full_completion_ratio * CONSISTENCY_FULL_COMPLETION_WEIGHT
        + streak_stability * CONSISTENCY_STREAK_STABILITY_WEIGHT
        + recent_momentum * CONSISTENCY_RECENT_MOMENTUM_WEIGHT
    ) * 100
    return round(score, 1)


def _habit_consistency_weight(habit, scheduled_total):
    if scheduled_total <= 0:
        return 0.0
    priority_weight = PRIORITY_SCORE_WEIGHTS.get(habit.priority, 1.0)
    return priority_weight * sqrt(scheduled_total)


def _empty_component_snapshot():
    return {
        component["key"]: {
            "value": 0.0,
            "points": 0.0,
        }
        for component in CONSISTENCY_SCORE_COMPONENTS
    }


def _aggregate_consistency_snapshot(habits, start_date, end_date):
    weighted_score = 0.0
    total_weight = 0.0
    scheduled_total = 0
    completed_total = 0
    component_totals = {component["key"]: 0.0 for component in CONSISTENCY_SCORE_COMPONENTS}

    for habit in habits:
        metrics = habit_performance_metrics(habit, start_date, end_date)
        if metrics["scheduled_total"] == 0:
            continue

        weight = _habit_consistency_weight(habit, metrics["scheduled_total"])
        if weight == 0:
            continue

        total_weight += weight
        scheduled_total += metrics["scheduled_total"]
        completed_total += metrics["completed_total"]
        weighted_score += metrics["consistency_score"] * weight
        for component in CONSISTENCY_SCORE_COMPONENTS:
            component_totals[component["key"]] += metrics[component["metric_key"]] * weight

    if total_weight == 0:
        return {
            "score": 0.0,
            "scheduled_total": scheduled_total,
            "completed_total": completed_total,
            "components": _empty_component_snapshot(),
        }

    components = {}
    for component in CONSISTENCY_SCORE_COMPONENTS:
        value = component_totals[component["key"]] / total_weight
        components[component["key"]] = {
            "value": round(value, 1),
            "points": round(value * component["weight"], 1),
        }

    return {
        "score": round(weighted_score / total_weight, 1),
        "scheduled_total": scheduled_total,
        "completed_total": completed_total,
        "components": components,
    }


def get_pending_habits_for_date(user, target_date):
    habits = list(
        Habit.objects.filter(user=user)
        .prefetch_related("pauses")
        .order_by("sort_order", "name")
    )
    completions = HabitCompletion.objects.filter(habit__in=habits, date=target_date)
    completion_map = {completion.habit_id: completion for completion in completions}

    pending = []
    for habit in habits:
        if not habit.is_scheduled_on(target_date):
            continue
        completion = completion_map.get(habit.id)
        completion_percentage = (
            float(completion.completion_percentage) if completion else 0.0
        )
        raw_value = None
        if completion and completion.raw_value is not None:
            raw_value = float(completion.raw_value)
            if habit.habit_type == Habit.HABIT_QUANTITATIVE:
                raw_value = int(raw_value)

        if completion is None or completion_percentage < 100:
            pending.append(
                {
                    "habit": habit,
                    "completion_percentage": completion_percentage,
                    "raw_value": raw_value,
                }
            )

    return pending


def build_overall_score_breakdown(
    habits,
    start_date,
    end_date,
    previous_start_date=None,
    previous_end_date=None,
):
    current = _aggregate_consistency_snapshot(habits, start_date, end_date)
    previous = None
    has_previous = False

    if (
        previous_start_date is not None
        and previous_end_date is not None
        and previous_start_date <= previous_end_date
    ):
        previous = _aggregate_consistency_snapshot(
            habits,
            previous_start_date,
            previous_end_date,
        )
        has_previous = previous["scheduled_total"] > 0

    component_rows = []
    for component in CONSISTENCY_SCORE_COMPONENTS:
        current_component = current["components"][component["key"]]
        previous_component = (
            previous["components"][component["key"]] if has_previous else None
        )
        component_rows.append(
            {
                "key": component["key"],
                "label": component["label"],
                "description": component["description"],
                "weight": int(component["weight"] * 100),
                "current_value": current_component["value"],
                "current_points": current_component["points"],
                "previous_value": (
                    previous_component["value"] if previous_component else None
                ),
                "previous_points": (
                    previous_component["points"] if previous_component else None
                ),
                "value_delta": (
                    round(
                        current_component["value"] - previous_component["value"],
                        1,
                    )
                    if previous_component
                    else None
                ),
                "points_delta": (
                    round(
                        current_component["points"] - previous_component["points"],
                        1,
                    )
                    if previous_component
                    else None
                ),
            }
        )

    return {
        "current_score": current["score"],
        "previous_score": previous["score"] if has_previous else None,
        "score_delta": (
            round(current["score"] - previous["score"], 1) if has_previous else None
        ),
        "has_previous": has_previous,
        "scheduled_total": current["scheduled_total"],
        "completed_total": current["completed_total"],
        "components": component_rows,
    }


def _habit_driver_snapshot(habit, metrics, previous_metrics, total_weight):
    weight = _habit_consistency_weight(habit, metrics["scheduled_total"])
    impact_points = 0.0
    drag_points = 0.0
    if total_weight:
        impact_points = (metrics["consistency_score"] * weight) / total_weight
        drag_points = ((100.0 - metrics["consistency_score"]) * weight) / total_weight

    score_delta = None
    if previous_metrics and previous_metrics["scheduled_total"] > 0:
        score_delta = round(
            metrics["consistency_score"] - previous_metrics["consistency_score"],
            1,
        )

    return {
        "habit": habit,
        "score": metrics["consistency_score"],
        "completion_rate": metrics["completion_rate"],
        "scheduled_total": metrics["scheduled_total"],
        "completed_total": metrics["completed_total"],
        "impact_points": round(impact_points, 1),
        "drag_points": round(drag_points, 1),
        "score_delta": score_delta,
    }


def build_habit_score_drivers(
    habits,
    start_date,
    end_date,
    previous_start_date=None,
    previous_end_date=None,
):
    current_rows = []
    total_weight = 0.0

    for habit in habits:
        metrics = habit_performance_metrics(habit, start_date, end_date)
        if metrics["scheduled_total"] == 0:
            continue
        weight = _habit_consistency_weight(habit, metrics["scheduled_total"])
        if weight == 0:
            continue
        total_weight += weight
        current_rows.append(
            {
                "habit": habit,
                "metrics": metrics,
            }
        )

    snapshots = []
    has_previous_window = (
        previous_start_date is not None
        and previous_end_date is not None
        and previous_start_date <= previous_end_date
    )
    for row in current_rows:
        previous_metrics = None
        if has_previous_window:
            previous_metrics = habit_performance_metrics(
                row["habit"],
                previous_start_date,
                previous_end_date,
            )
        snapshots.append(
            _habit_driver_snapshot(
                row["habit"],
                row["metrics"],
                previous_metrics,
                total_weight,
            )
        )

    if not snapshots:
        return {
            "booster": None,
            "drag": None,
            "improved": None,
            "declined": None,
        }

    movement_snapshots = [
        snapshot for snapshot in snapshots if snapshot["score_delta"] is not None
    ]
    improved_snapshots = [
        snapshot for snapshot in movement_snapshots if snapshot["score_delta"] > 0
    ]
    declined_snapshots = [
        snapshot for snapshot in movement_snapshots if snapshot["score_delta"] < 0
    ]

    return {
        "booster": max(snapshots, key=lambda item: item["impact_points"]),
        "drag": max(snapshots, key=lambda item: item["drag_points"]),
        "improved": (
            max(improved_snapshots, key=lambda item: item["score_delta"])
            if improved_snapshots
            else None
        ),
        "declined": (
            min(declined_snapshots, key=lambda item: item["score_delta"])
            if declined_snapshots
            else None
        ),
    }


def build_category_analytics(habits, start_date, end_date):
    summaries = []
    best_category = None
    weakest_category = None

    categories = list(HabitCategory.objects.all())
    habit_category_ids = {
        habit.id: {category.id for category in habit.categories.all()}
        for habit in habits
    }

    for category in categories:
        category_habits = [
            habit
            for habit in habits
            if category.id in habit_category_ids.get(habit.id, set())
        ]
        total_scheduled = 0
        total_completed = 0
        total_completion = 0.0
        weighted_score = 0.0
        total_weight = 0.0

        for habit in category_habits:
            metrics = habit_performance_metrics(habit, start_date, end_date)
            if metrics["scheduled_total"] == 0:
                continue

            total_scheduled += metrics["scheduled_total"]
            total_completed += metrics["completed_total"]
            total_completion += metrics["completion_rate"] * metrics["scheduled_total"]

            weight = _habit_consistency_weight(habit, metrics["scheduled_total"])
            total_weight += weight
            weighted_score += metrics["consistency_score"] * weight

        completion_rate = (
            round(total_completion / total_scheduled, 1) if total_scheduled else 0.0
        )
        consistency_score = (
            round(weighted_score / total_weight, 1) if total_weight else 0.0
        )
        summary = {
            "key": category.key,
            "label": category.label,
            "habit_count": len(category_habits),
            "scheduled_total": total_scheduled,
            "completed_total": total_completed,
            "completion_rate": completion_rate,
            "consistency_score": consistency_score,
            "is_best": False,
            "is_weakest": False,
        }
        summaries.append(summary)

        if total_scheduled == 0:
            continue
        if best_category is None or consistency_score > best_category["consistency_score"]:
            best_category = summary
        if weakest_category is None or consistency_score < weakest_category["consistency_score"]:
            weakest_category = summary

    if best_category:
        best_category["is_best"] = True
    if weakest_category:
        weakest_category["is_weakest"] = True

    return {
        "summaries": summaries,
        "best": best_category,
        "weakest": weakest_category,
    }


def habit_performance_metrics(habit, start_date, end_date, completion_map=None, value_map=None):
    scheduled_dates = list(iter_scheduled_dates(habit, start_date, end_date))

    if completion_map is None or value_map is None:
        completion_map, value_map = get_completion_maps(habit, start_date, end_date)

    scheduled_dates = _scored_scheduled_dates(scheduled_dates, completion_map, end_date)
    scheduled_total = len(scheduled_dates)

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
            "completion_quality": 0.0,
            "full_completion_reliability": 0.0,
            "recent_momentum": 0.0,
        }

    streaks = _streak_metrics(scheduled_dates, completion_map)
    completed_total = streaks["completed_total"]
    missed_total = scheduled_total - completed_total
    total_completion = 0.0
    total_value = 0.0
    for scheduled_date in scheduled_dates:
        total_completion += _clamped_percentage(completion_map.get(scheduled_date, 0))
        total_value += value_map.get(scheduled_date, 0) or 0
    average_completion = round(total_completion / scheduled_total, 1)
    completion_rate = average_completion
    if habit.habit_type == Habit.HABIT_QUANTITATIVE:
        average_value = round(total_value / scheduled_total)
    else:
        average_value = round(total_value / scheduled_total, 2)

    completion_quality = (total_completion / scheduled_total) / 100
    full_completion_ratio = completed_total / scheduled_total
    streak_stability = _rhythm_stability(scheduled_dates, completion_map)
    recent_momentum = _recent_momentum(scheduled_dates, completion_map)
    consistency_score = _consistency_score(
        completion_quality=completion_quality,
        full_completion_ratio=full_completion_ratio,
        streak_stability=streak_stability,
        recent_momentum=recent_momentum,
    )

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
        "completion_quality": round(completion_quality * 100, 1),
        "full_completion_reliability": round(full_completion_ratio * 100, 1),
        "recent_momentum": round(recent_momentum * 100, 1),
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
        "full_completion_reliability": metrics["full_completion_reliability"],
    }


def calculate_overall_consistency(habits, start_date, end_date):
    weighted_score = 0.0
    total_weight = 0.0

    for habit in habits:
        metrics = habit_performance_metrics(habit, start_date, end_date)
        if metrics["scheduled_total"] == 0:
            continue
        weight = _habit_consistency_weight(habit, metrics["scheduled_total"])
        total_weight += weight
        weighted_score += metrics["consistency_score"] * weight

    if total_weight == 0:
        return 0.0
    return round(weighted_score / total_weight, 1)


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
    consistency_weighted_score = 0.0
    consistency_total_weight = 0.0

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
        consistency_weight = _habit_consistency_weight(habit, metrics["scheduled_total"])
        consistency_weighted_score += metrics["consistency_score"] * consistency_weight
        consistency_total_weight += consistency_weight

    completion_rate = round(total_completion / total_scheduled, 1) if total_scheduled else 0.0
    average_current_streak = (
        round(sum_current_streak / tracked_habits, 1) if tracked_habits else 0.0
    )
    average_max_streak = round(sum_max_streak / tracked_habits, 1) if tracked_habits else 0.0
    consistency_score = (
        round(consistency_weighted_score / consistency_total_weight, 1)
        if consistency_total_weight
        else 0.0
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

    current_week_start = today - timedelta(days=today.weekday())

    reports = []
    for week_index in reversed(range(weeks)):
        period_start = current_week_start - timedelta(days=week_index * 7)
        period_end = period_start + timedelta(days=6)
        if period_end > today:
            period_end = today
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

    active_pause = habit.active_pause()
    if active_pause and active_pause.start_date <= from_date:
        return None

    if habit.schedule_type == Habit.SCHEDULE_DAILY:
        candidate = from_date
    elif habit.schedule_type == Habit.SCHEDULE_WEEKLY:
        interval = max(1, habit.weekly_interval) * 7
        delta_days = (from_date - habit.start_date).days
        remainder = delta_days % interval
        if remainder == 0:
            candidate = from_date
        else:
            candidate = from_date + timedelta(days=interval - remainder)
    elif habit.schedule_type == Habit.SCHEDULE_INTERVAL:
        interval = max(1, habit.interval_days)
        delta_days = (from_date - habit.start_date).days
        remainder = delta_days % interval
        if remainder == 0:
            candidate = from_date
        else:
            candidate = from_date + timedelta(days=interval - remainder)
    elif habit.schedule_type == Habit.SCHEDULE_DAYS:
        days = habit.get_days_of_week_set()
        if not days:
            return None
        candidate = None
        for offset in range(0, 7):
            current = from_date + timedelta(days=offset)
            if current.weekday() in days:
                candidate = current
                break
    else:
        return None

    if candidate is None:
        return None

    def advance(next_date):
        if habit.schedule_type == Habit.SCHEDULE_DAILY:
            return next_date + timedelta(days=1)
        if habit.schedule_type == Habit.SCHEDULE_WEEKLY:
            return next_date + timedelta(days=max(1, habit.weekly_interval) * 7)
        if habit.schedule_type == Habit.SCHEDULE_INTERVAL:
            return next_date + timedelta(days=max(1, habit.interval_days))
        if habit.schedule_type == Habit.SCHEDULE_DAYS:
            days = habit.get_days_of_week_set()
            if not days:
                return None
            for offset in range(1, 8):
                current = next_date + timedelta(days=offset)
                if current.weekday() in days:
                    return current
            return None
        return None

    while habit.is_paused_on(candidate):
        if active_pause and active_pause.start_date <= candidate:
            return None
        candidate = advance(candidate)
        if candidate is None:
            return None

    return candidate
