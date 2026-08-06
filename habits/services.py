from calendar import monthrange
from dataclasses import dataclass
from datetime import date, timedelta
from math import isfinite, sqrt

from django.db.models import Q
from django.utils import timezone

from .models import Habit, HabitCategory, HabitCompletion
CONSISTENCY_COMPLETION_QUALITY_WEIGHT = 0.45
CONSISTENCY_FULL_COMPLETION_WEIGHT = 0.25
CONSISTENCY_RHYTHM_WEIGHT = 0.15
CONSISTENCY_RECENT_MOMENTUM_WEIGHT = 0.15
RHYTHM_SESSION_WINDOW = 7
MOMENTUM_SESSION_WINDOW = 6
RHYTHM_CONTINUATION_THRESHOLD = 50.0
MOMENTUM_STABLE_BAND = 5.0
MOMENTUM_FULL_SIGNAL_CHANGE = 50.0
RHYTHM_COVERAGE_WEIGHT = 0.8
RHYTHM_CONTINUITY_WEIGHT = 0.2
RECENT_METRIC_CONFIDENCE_SESSIONS = 3
PRIORITY_WEIGHTS = {
    Habit.PRIORITY_HIGH: 1.3,
    Habit.PRIORITY_MEDIUM: 1.0,
    Habit.PRIORITY_LOW: 0.8,
}
PRIORITY_LABELS = dict(Habit.PRIORITY_CHOICES)
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
        "label": "Consistency rhythm",
        "metric_key": "streak_stability",
        "weight": CONSISTENCY_RHYTHM_WEIGHT,
        "description": "Recent success coverage and consecutive successful sessions.",
    },
    {
        "key": "recent_momentum",
        "label": "Recent momentum",
        "metric_key": "recent_momentum",
        "weight": CONSISTENCY_RECENT_MOMENTUM_WEIGHT,
        "description": "Confidence-adjusted direction across recent scheduled sessions.",
    },
)


def should_prompt_daily_recap(previous_login, today=None):
    if today is None:
        today = timezone.localdate()
    if previous_login is None:
        return True
    return timezone.localdate(previous_login) < today


def can_update_progress_on(target_date, today=None):
    """Return whether normal progress editing is allowed for ``target_date``."""
    if today is None:
        today = timezone.localdate()
    return today - timedelta(days=1) <= target_date <= today


def daily_recap_target_date(today=None):
    """Return the only date that may be updated through daily recap."""
    if today is None:
        today = timezone.localdate()
    return today - timedelta(days=1)


@dataclass(frozen=True)
class HabitPlanConfig:
    """The immutable scheduling and scoring configuration active on a date."""

    effective_from: date
    schedule_anchor: date
    schedule_type: str
    interval_days: int
    weekly_interval: int
    days_of_week: frozenset
    priority: str
    category_ids: frozenset

    @property
    def priority_label(self):
        return PRIORITY_LABELS.get(self.priority, self.priority.title())

    @property
    def schedule_summary(self):
        if self.schedule_type == Habit.SCHEDULE_DAILY:
            return "Every day"
        if self.schedule_type == Habit.SCHEDULE_WEEKLY:
            day_label = self.schedule_anchor.strftime("%A")
            if self.weekly_interval == 1:
                return f"Every week on {day_label}"
            return f"Every {self.weekly_interval} weeks on {day_label}"
        if self.schedule_type == Habit.SCHEDULE_INTERVAL:
            if self.interval_days == 1:
                return "Every day"
            return f"Every {self.interval_days} days"
        if self.schedule_type == Habit.SCHEDULE_DAYS:
            labels = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
            ordered = [
                labels[index]
                for index in range(7)
                if index in self.days_of_week
            ]
            return "Every " + ", ".join(ordered) if ordered else "Specific days"
        return ""


@dataclass(frozen=True)
class ScheduledOccurrence:
    """One scheduled session with the configuration that created it."""

    date: date
    config: HabitPlanConfig

    @property
    def priority(self):
        return self.config.priority

    @property
    def priority_weight(self):
        return PRIORITY_WEIGHTS.get(self.priority, 1.0)

    @property
    def category_ids(self):
        return self.config.category_ids

    @property
    def priority_label(self):
        return self.config.priority_label

    @property
    def schedule_summary(self):
        return self.config.schedule_summary


def _related_category_ids(instance):
    prefetched = getattr(instance, "_prefetched_objects_cache", None) or {}
    if "categories" in prefetched:
        return frozenset(category.pk for category in prefetched["categories"])
    if getattr(instance, "pk", None) is None:
        return frozenset()
    return frozenset(instance.categories.values_list("pk", flat=True))


def _mutable_habit_plan_config(habit):
    return HabitPlanConfig(
        effective_from=habit.start_date,
        schedule_anchor=habit.start_date,
        schedule_type=habit.schedule_type,
        interval_days=max(1, habit.interval_days or 1),
        weekly_interval=max(1, habit.weekly_interval or 1),
        days_of_week=frozenset(habit.get_days_of_week_set()),
        priority=habit.priority,
        category_ids=_related_category_ids(habit),
    )


def _habit_plan_configs(habit):
    """Return effective-dated plans, falling back for unversioned habits."""
    if getattr(habit, "pk", None) is None or not hasattr(habit, "plan_versions"):
        return (_mutable_habit_plan_config(habit),)

    prefetched = getattr(habit, "_prefetched_objects_cache", None) or {}
    if "plan_versions" in prefetched:
        versions = sorted(
            prefetched["plan_versions"],
            key=lambda version: (version.effective_from, version.pk or 0),
        )
    else:
        versions = list(habit.plan_versions.prefetch_related("categories").all())

    if not versions:
        return (_mutable_habit_plan_config(habit),)

    configs = tuple(
        HabitPlanConfig(
            effective_from=version.effective_from,
            schedule_anchor=version.schedule_anchor,
            schedule_type=version.schedule_type,
            interval_days=max(1, version.interval_days or 1),
            weekly_interval=max(1, version.weekly_interval or 1),
            days_of_week=frozenset(version.get_days_of_week_set()),
            priority=version.priority,
            category_ids=_related_category_ids(version),
        )
        for version in versions
    )
    return configs


def habit_tracking_start(habit):
    """Return the immutable first date on which a habit can be tracked."""
    configs = _habit_plan_configs(habit)
    candidates = []
    for index, config in enumerate(configs):
        next_effective = (
            configs[index + 1].effective_from
            if index + 1 < len(configs)
            else None
        )
        candidate = _next_config_date(
            config,
            max(config.effective_from, config.schedule_anchor),
        )
        if candidate is not None and (
            next_effective is None or candidate < next_effective
        ):
            candidates.append(candidate)
    if candidates:
        return min(candidates)
    return max(configs[-1].effective_from, configs[-1].schedule_anchor)


def resolve_habit_plan_on(habit, target_date):
    """Return the habit plan configuration effective on target_date."""
    active = None
    for config in _habit_plan_configs(habit):
        if config.effective_from > target_date:
            break
        active = config
    return active


def _schedule_interval_days(config):
    if config.schedule_type == Habit.SCHEDULE_WEEKLY:
        return max(1, config.weekly_interval) * 7
    if config.schedule_type == Habit.SCHEDULE_INTERVAL:
        return max(1, config.interval_days)
    return None


def _next_aligned_date(config, from_date, interval):
    from_date = max(from_date, config.schedule_anchor)
    delta_days = (from_date - config.schedule_anchor).days
    remainder = delta_days % interval
    if remainder == 0:
        return from_date
    return from_date + timedelta(days=interval - remainder)


def _next_config_date(config, from_date):
    from_date = max(from_date, config.effective_from, config.schedule_anchor)
    if config.schedule_type == Habit.SCHEDULE_DAILY:
        return from_date

    interval = _schedule_interval_days(config)
    if interval is not None:
        return _next_aligned_date(config, from_date, interval)

    if config.schedule_type == Habit.SCHEDULE_DAYS:
        if not config.days_of_week:
            return None
        for offset in range(7):
            candidate = from_date + timedelta(days=offset)
            if candidate.weekday() in config.days_of_week:
                return candidate
    return None


def _iter_config_dates(config, start_date, end_date):
    start_date = max(start_date, config.effective_from, config.schedule_anchor)
    if end_date < start_date:
        return

    if config.schedule_type == Habit.SCHEDULE_DAILY:
        current = start_date
        step = timedelta(days=1)
    else:
        interval = _schedule_interval_days(config)
        if interval is not None:
            current = _next_aligned_date(config, start_date, interval)
            step = timedelta(days=interval)
        elif config.schedule_type == Habit.SCHEDULE_DAYS:
            if not config.days_of_week:
                return
            current = start_date
            step = timedelta(days=1)
        else:
            return

    while current <= end_date:
        if (
            config.schedule_type != Habit.SCHEDULE_DAYS
            or current.weekday() in config.days_of_week
        ):
            yield current
        current += step


def iter_scheduled_occurrences(habit, start_date, end_date):
    """Yield scheduled sessions with their occurrence-time configuration."""
    if end_date < start_date:
        return

    configs = _habit_plan_configs(habit)
    if end_date < configs[0].effective_from:
        return

    pause_ranges = _pause_ranges_for_habit(habit, start_date, end_date)
    for index, config in enumerate(configs):
        next_effective = (
            configs[index + 1].effective_from if index + 1 < len(configs) else None
        )
        segment_start = max(start_date, config.effective_from)
        segment_end = end_date
        if next_effective is not None:
            segment_end = min(segment_end, next_effective - timedelta(days=1))
        if segment_end < segment_start:
            continue

        for scheduled_date in _iter_config_dates(config, segment_start, segment_end):
            if not _is_paused_on_date(scheduled_date, pause_ranges):
                yield ScheduledOccurrence(scheduled_date, config)


def scheduled_occurrence_on(habit, target_date):
    """Return the scheduled occurrence on a date, or None."""
    return next(iter_scheduled_occurrences(habit, target_date, target_date), None)


def is_habit_scheduled_on(habit, target_date):
    """Version-aware public replacement for Habit.is_scheduled_on."""
    return scheduled_occurrence_on(habit, target_date) is not None


def iter_scheduled_dates(habit, start_date, end_date):
    for occurrence in iter_scheduled_occurrences(habit, start_date, end_date):
        yield occurrence.date


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

    prefetched = getattr(habit, "_prefetched_objects_cache", None)
    if prefetched and "pauses" in prefetched:
        return [
            (pause.start_date, pause.end_date)
            for pause in prefetched["pauses"]
            if pause.start_date <= end_date
            and (pause.end_date is None or pause.end_date > start_date)
        ]

    pauses = habit.pauses.filter(start_date__lte=end_date).filter(
        Q(end_date__isnull=True) | Q(end_date__gt=start_date)
    )
    return list(pauses.values_list("start_date", "end_date"))


def _is_paused_on_date(target_date, pause_ranges):
    for start_date, end_date in pause_ranges:
        if start_date <= target_date and (end_date is None or target_date < end_date):
            return True
    return False


def _is_completed(percentage_value):
    return _clamped_percentage(percentage_value) >= 100


def _clamped_percentage(percentage_value):
    try:
        percentage = float(percentage_value or 0)
    except (TypeError, ValueError):
        return 0.0
    if not isfinite(percentage):
        return 0.0
    return max(0.0, min(100.0, percentage))


def _priority_weight(subject):
    """Return the shared weight for a habit, plan, or scheduled occurrence."""
    explicit_weight = getattr(subject, "priority_weight", None)
    if explicit_weight is not None:
        return explicit_weight
    return PRIORITY_WEIGHTS.get(getattr(subject, "priority", ""), 1.0)


def weighted_completion_rate_from_totals(
    weighted_completion_total,
    priority_weight_total,
):
    """Return a rate from canonical occurrence-weighted numerator/denominator."""
    try:
        weighted_completion_total = float(weighted_completion_total or 0)
        priority_weight_total = float(priority_weight_total or 0)
    except (TypeError, ValueError):
        return 0.0
    if (
        not isfinite(weighted_completion_total)
        or not isfinite(priority_weight_total)
        or priority_weight_total <= 0
    ):
        return 0.0
    weighted_completion_total = max(
        0.0,
        min(100.0 * priority_weight_total, weighted_completion_total),
    )
    return round(weighted_completion_total / priority_weight_total, 1)


def weighted_completion_rate(progress_entries):
    """Return the one canonical completion rate used throughout the site.

    Each entry is ``(habit, completion_total, scheduled_count)``. The
    ``completion_total`` is the sum of the 0-100 completion percentages for
    those scheduled occurrences. Passing one occurrence therefore uses a
    count of ``1`` and its individual completion percentage as the total.

    Every occurrence is weighted by the supplied habit/configuration priority
    (High 1.3, Medium 1.0, Low 0.8), partial percentages contribute
    proportionally, and missing occurrences are represented by zero completion
    in the supplied total.
    """
    weighted_completion_total = 0.0
    weighted_scheduled_total = 0.0

    for habit, completion_total, scheduled_count in progress_entries:
        try:
            scheduled_count = float(scheduled_count)
        except (TypeError, ValueError):
            continue
        if not isfinite(scheduled_count) or scheduled_count <= 0:
            continue

        try:
            completion_total = float(completion_total or 0)
        except (TypeError, ValueError):
            completion_total = 0.0
        if not isfinite(completion_total):
            completion_total = 0.0
        completion_total = max(
            0.0,
            min(100.0 * scheduled_count, completion_total),
        )

        weight = _priority_weight(habit)
        weighted_completion_total += weight * completion_total
        weighted_scheduled_total += weight * scheduled_count

    return weighted_completion_rate_from_totals(
        weighted_completion_total,
        weighted_scheduled_total,
    )


def _is_successful_continuation(percentage_value):
    return _clamped_percentage(percentage_value) > RHYTHM_CONTINUATION_THRESHOLD


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


def _consistency_rhythm(scheduled_dates, completion_map):
    """Return confidence-adjusted coverage and continuity for recent sessions.

    Progress above 50% is successful; 50% or less, including an unlogged
    session, is a failure. Coverage supplies 80% of the factor and consecutive
    successful-session transitions supply 20%. For ``k`` observations,
    confidence is ``min(1, k / 3)``. With fewer than three observations, the
    result is shrunk toward 50% so one success cannot claim a perfect rhythm.
    """
    recent_dates = scheduled_dates[-RHYTHM_SESSION_WINDOW:]
    if not recent_dates:
        return 0.0

    success_flags = [
        _is_successful_continuation(completion_map.get(scheduled_date, 0))
        for scheduled_date in recent_dates
    ]
    successful_sessions = sum(success_flags)
    if successful_sessions == 0:
        return 0.0

    session_count = len(success_flags)
    coverage = successful_sessions / session_count
    if session_count == 1:
        continuity = coverage
    else:
        successful_pairs = sum(
            1
            for previous, current in zip(success_flags, success_flags[1:])
            if previous and current
        )
        continuity = successful_pairs / (session_count - 1)

    raw_rhythm = (
        coverage * RHYTHM_COVERAGE_WEIGHT
        + continuity * RHYTHM_CONTINUITY_WEIGHT
    )
    confidence = min(
        1.0,
        session_count / RECENT_METRIC_CONFIDENCE_SESSIONS,
    )
    return max(0.0, min(1.0, 0.5 + confidence * (raw_rhythm - 0.5)))


def _momentum_change_signal(previous_value, current_value):
    """Normalize one percentage-point change to the -1..1 momentum scale."""
    change = current_value - previous_value
    magnitude = abs(change)
    if magnitude <= MOMENTUM_STABLE_BAND:
        return 0.0

    meaningful_change = magnitude - MOMENTUM_STABLE_BAND
    full_signal_range = MOMENTUM_FULL_SIGNAL_CHANGE - MOMENTUM_STABLE_BAND
    signal = meaningful_change / full_signal_range
    if change < 0:
        signal *= -1
    return max(-1.0, min(1.0, signal))


def _recent_momentum(scheduled_dates, completion_map):
    """Return a cadence-fair trend factor across six scheduled sessions.

    Momentum is neutral at 50%. Changes of five percentage points or less are
    treated as stable noise. Larger changes are normalized so a 50-point
    improvement or decline produces a full positive or negative signal. Newer
    transitions receive larger ordinal weights, keeping different habit cadences
    comparable and preventing calendar gaps from affecting the result. Fewer
    than three observations shrink the trend toward neutral. The latest
    session's progress supplies the evidence factor, preventing an old high
    result from propping up sustained low recent performance.
    """
    recent_dates = scheduled_dates[-MOMENTUM_SESSION_WINDOW:]
    if not recent_dates:
        return 0.0

    completion_values = [
        _clamped_percentage(completion_map.get(scheduled_date, 0))
        for scheduled_date in recent_dates
    ]
    evidence = min(
        1.0,
        completion_values[-1] / RHYTHM_CONTINUATION_THRESHOLD,
    )
    if len(completion_values) == 1:
        return 0.5 * evidence

    if evidence == 0:
        return 0.0

    weighted_signal = 0.0
    total_weight = 0.0
    for index in range(1, len(completion_values)):
        weight = float(index)
        previous_value = completion_values[index - 1]
        current_value = completion_values[index]
        signal = _momentum_change_signal(previous_value, current_value)
        total_weight += weight
        weighted_signal += signal * weight

    if total_weight == 0:
        return 0.5
    average_signal = weighted_signal / total_weight
    confidence = min(
        1.0,
        len(completion_values) / RECENT_METRIC_CONFIDENCE_SESSIONS,
    )
    raw_momentum = max(
        0.0,
        min(1.0, 0.5 + (average_signal * 0.5 * confidence)),
    )
    return raw_momentum * evidence


def _consistency_score(
    completion_quality,
    full_completion_ratio,
    consistency_rhythm,
    recent_momentum,
):
    score = (
        completion_quality * CONSISTENCY_COMPLETION_QUALITY_WEIGHT
        + full_completion_ratio * CONSISTENCY_FULL_COMPLETION_WEIGHT
        + consistency_rhythm * CONSISTENCY_RHYTHM_WEIGHT
        + recent_momentum * CONSISTENCY_RECENT_MOMENTUM_WEIGHT
    ) * 100
    return round(score, 1)


def _habit_consistency_weight(
    habit,
    scheduled_total,
    priority_weight_total=None,
):
    if scheduled_total <= 0:
        return 0.0
    if priority_weight_total is None:
        priority_weight_total = _priority_weight(habit) * scheduled_total
    try:
        priority_weight_total = float(priority_weight_total)
    except (TypeError, ValueError):
        return 0.0
    if not isfinite(priority_weight_total) or priority_weight_total <= 0:
        return 0.0
    average_priority_weight = priority_weight_total / scheduled_total
    return average_priority_weight * sqrt(scheduled_total)


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

        weight = _habit_consistency_weight(
            habit,
            metrics["scheduled_total"],
            metrics["priority_weight_total"],
        )
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
        .prefetch_related(
            "pauses",
            "plan_versions__categories",
        )
        .order_by("sort_order", "name")
    )
    completions = HabitCompletion.objects.filter(habit__in=habits, date=target_date)
    completion_map = {completion.habit_id: completion for completion in completions}

    pending = []
    for habit in habits:
        occurrence = scheduled_occurrence_on(habit, target_date)
        if occurrence is None:
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
                    "schedule_summary": occurrence.schedule_summary,
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
    weight = _habit_consistency_weight(
        habit,
        metrics["scheduled_total"],
        metrics["priority_weight_total"],
    )
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
        weight = _habit_consistency_weight(
            habit,
            metrics["scheduled_total"],
            metrics["priority_weight_total"],
        )
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
    category_metrics = {category.id: [] for category in categories}

    for habit in habits:
        occurrences = list(
            iter_scheduled_occurrences(habit, start_date, end_date)
        )
        if not occurrences:
            continue

        completion_map, value_map = get_completion_maps(
            habit,
            start_date,
            end_date,
        )
        occurrences_by_category = {}
        for occurrence in occurrences:
            for category_id in occurrence.category_ids:
                occurrences_by_category.setdefault(category_id, []).append(occurrence)

        for category_id, attributed_occurrences in occurrences_by_category.items():
            if category_id not in category_metrics:
                continue
            metrics = _performance_metrics_for_occurrences(
                habit,
                attributed_occurrences,
                completion_map,
                value_map,
            )
            category_metrics[category_id].append((habit, metrics))

    for category in categories:
        attributed_rows = category_metrics[category.id]
        total_scheduled = 0
        total_completed = 0
        weighted_completion_total = 0.0
        priority_weight_total = 0.0
        weighted_score = 0.0
        total_weight = 0.0

        for habit, metrics in attributed_rows:
            total_scheduled += metrics["scheduled_total"]
            total_completed += metrics["completed_total"]
            weighted_completion_total += metrics["weighted_completion_total"]
            priority_weight_total += metrics["priority_weight_total"]

            weight = _habit_consistency_weight(
                habit,
                metrics["scheduled_total"],
                metrics["priority_weight_total"],
            )
            total_weight += weight
            weighted_score += metrics["consistency_score"] * weight

        completion_rate = weighted_completion_rate_from_totals(
            weighted_completion_total,
            priority_weight_total,
        )
        consistency_score = (
            round(weighted_score / total_weight, 1) if total_weight else 0.0
        )
        summary = {
            "key": category.key,
            "label": category.label,
            "habit_count": len(attributed_rows),
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


def _performance_metrics_for_occurrences(
    habit,
    occurrences,
    completion_map,
    value_map,
):
    scheduled_dates = [occurrence.date for occurrence in occurrences]
    scheduled_total = len(scheduled_dates)

    if scheduled_total == 0:
        return {
            "scheduled_total": 0,
            "completed_total": 0,
            "missed_total": 0,
            "completion_total": 0.0,
            "weighted_completion_total": 0.0,
            "priority_weight_total": 0.0,
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
    weighted_completion_total = 0.0
    priority_weight_total = 0.0
    total_value = 0.0
    for occurrence in occurrences:
        completion = _clamped_percentage(completion_map.get(occurrence.date, 0))
        total_completion += completion
        weighted_completion_total += completion * occurrence.priority_weight
        priority_weight_total += occurrence.priority_weight
        total_value += value_map.get(occurrence.date, 0) or 0

    completion_rate = weighted_completion_rate_from_totals(
        weighted_completion_total,
        priority_weight_total,
    )
    average_completion = completion_rate
    if habit.habit_type == Habit.HABIT_QUANTITATIVE:
        average_value = round(total_value / scheduled_total)
    else:
        average_value = round(total_value / scheduled_total, 2)

    completion_quality = (total_completion / scheduled_total) / 100
    full_completion_ratio = completed_total / scheduled_total
    consistency_rhythm = _consistency_rhythm(scheduled_dates, completion_map)
    recent_momentum = _recent_momentum(scheduled_dates, completion_map)
    consistency_score = _consistency_score(
        completion_quality=completion_quality,
        full_completion_ratio=full_completion_ratio,
        consistency_rhythm=consistency_rhythm,
        recent_momentum=recent_momentum,
    )

    return {
        "scheduled_total": scheduled_total,
        "completed_total": completed_total,
        "missed_total": missed_total,
        "completion_total": total_completion,
        "weighted_completion_total": weighted_completion_total,
        "priority_weight_total": priority_weight_total,
        "completion_rate": completion_rate,
        "current_streak": streaks["current_streak"],
        "max_streak": streaks["max_streak"],
        "streak_stability": round(consistency_rhythm * 100, 1),
        "consistency_score": consistency_score,
        "average_completion": average_completion,
        "average_value": average_value,
        "completion_quality": round(completion_quality * 100, 1),
        "full_completion_reliability": round(full_completion_ratio * 100, 1),
        "recent_momentum": round(recent_momentum * 100, 1),
    }


def habit_performance_metrics(
    habit,
    start_date,
    end_date,
    completion_map=None,
    value_map=None,
):
    occurrences = list(iter_scheduled_occurrences(habit, start_date, end_date))

    loaded_completion_map = None
    loaded_value_map = None
    if completion_map is None or value_map is None:
        loaded_completion_map, loaded_value_map = get_completion_maps(
            habit,
            start_date,
            end_date,
        )

    if completion_map is None:
        completion_map = loaded_completion_map or {}
    if value_map is None:
        value_map = loaded_value_map or {}

    return _performance_metrics_for_occurrences(
        habit,
        occurrences,
        completion_map,
        value_map,
    )


def calculate_streaks(habit, up_to_date=None):
    if up_to_date is None:
        up_to_date = timezone.localdate()

    metrics = habit_performance_metrics(
        habit,
        habit_tracking_start(habit),
        up_to_date,
    )
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
        weight = _habit_consistency_weight(
            habit,
            metrics["scheduled_total"],
            metrics["priority_weight_total"],
        )
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
    weighted_completion_total = 0.0
    priority_weight_total = 0.0
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
        weighted_completion_total += metrics["weighted_completion_total"]
        priority_weight_total += metrics["priority_weight_total"]
        sum_current_streak += metrics["current_streak"]
        sum_max_streak += metrics["max_streak"]
        consistency_weight = _habit_consistency_weight(
            habit,
            metrics["scheduled_total"],
            metrics["priority_weight_total"],
        )
        consistency_weighted_score += metrics["consistency_score"] * consistency_weight
        consistency_total_weight += consistency_weight

    completion_rate = weighted_completion_rate_from_totals(
        weighted_completion_total,
        priority_weight_total,
    )
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

    configs = _habit_plan_configs(habit)
    from_date = max(from_date, habit_tracking_start(habit))

    prefetched = getattr(habit, "_prefetched_objects_cache", None) or {}
    if "pauses" in prefetched:
        pause_ranges = sorted(
            (
                (pause.start_date, pause.end_date)
                for pause in prefetched["pauses"]
                if pause.end_date is None or pause.end_date > from_date
            ),
            key=lambda pause_range: pause_range[0],
        )
    else:
        pause_ranges = list(
            habit.pauses.filter(
                Q(end_date__isnull=True) | Q(end_date__gt=from_date)
            )
            .order_by("start_date")
            .values_list("start_date", "end_date")
        )

    for index, config in enumerate(configs):
        next_effective = (
            configs[index + 1].effective_from if index + 1 < len(configs) else None
        )
        search_from = max(from_date, config.effective_from, config.schedule_anchor)
        segment_end = (
            next_effective - timedelta(days=1)
            if next_effective is not None
            else None
        )
        if segment_end is not None and search_from > segment_end:
            continue

        while True:
            candidate = _next_config_date(config, search_from)
            if candidate is None:
                break
            if segment_end is not None and candidate > segment_end:
                break

            containing_pause = next(
                (
                    pause_range
                    for pause_range in pause_ranges
                    if pause_range[0] <= candidate
                    and (
                        pause_range[1] is None
                        or candidate < pause_range[1]
                    )
                ),
                None,
            )
            if containing_pause is None:
                return candidate
            if containing_pause[1] is None:
                return None
            search_from = containing_pause[1]

    return None


def compute_user_metrics(habits, start_date, end_date):
    """Single source of truth for per-user cross-habit aggregation.

    Returns a dict with ``per_habit`` (list of habit cards with the same shape
    used by the dashboard), ``aggregate`` (overall totals), and
    ``consistency_score`` (priority- and frequency-weighted).
    """
    per_habit = []
    total_scheduled = 0
    weighted_completion_total = 0.0
    priority_weight_total = 0.0
    total_completed = 0
    weighted_score = 0.0
    total_weight = 0.0
    best_streak = 0

    for habit in habits:
        metrics = habit_performance_metrics(habit, start_date, end_date)
        per_habit.append(
            {
                "habit": habit,
                "metrics": metrics,
                "scheduled": metrics["scheduled_total"],
                "completed": metrics["completed_total"],
                "rate": metrics["completion_rate"],
                "consistency": metrics["consistency_score"],
                "completion_quality": metrics["completion_quality"],
                "full_completion": metrics["full_completion_reliability"],
                "rhythm_stability": metrics["streak_stability"],
                "recent_momentum": metrics["recent_momentum"],
            }
        )
        if metrics["scheduled_total"] == 0:
            continue
        total_scheduled += metrics["scheduled_total"]
        weighted_completion_total += metrics["weighted_completion_total"]
        priority_weight_total += metrics["priority_weight_total"]
        total_completed += metrics["completed_total"]
        if metrics["max_streak"] > best_streak:
            best_streak = metrics["max_streak"]
        weight = _habit_consistency_weight(
            habit,
            metrics["scheduled_total"],
            metrics["priority_weight_total"],
        )
        total_weight += weight
        weighted_score += metrics["consistency_score"] * weight

    overall_rate = weighted_completion_rate_from_totals(
        weighted_completion_total,
        priority_weight_total,
    )
    consistency_score = (
        round(weighted_score / total_weight, 1) if total_weight else 0.0
    )

    return {
        "per_habit": per_habit,
        "aggregate": {
            "total_scheduled": total_scheduled,
            "total_completed": total_completed,
            "completion_rate": overall_rate,
        },
        "consistency_score": consistency_score,
        "best_streak": best_streak,
    }


def compute_today_metrics(user, target_date):
    """Single source of truth for the today-page summary.

    Returns ``scheduled_count``, ``completed_count``, ``completion_rate``
    (priority-weighted), and the per-habit rows used by the template.
    """
    from .models import HabitCompletion  # local import to avoid cycle

    habits = list(
        Habit.objects.filter(user=user)
        .prefetch_related(
            "categories",
            "pauses",
            "plan_versions__categories",
        )
        .order_by("sort_order", "name")
    )
    completions = HabitCompletion.objects.filter(habit__in=habits, date=target_date)
    completion_map = {completion.habit_id: completion for completion in completions}

    scheduled_habits = []
    completion_lookup = {}
    occurrence_lookup = {}
    completion_entries = []
    completed_count = 0
    for habit in habits:
        occurrence = scheduled_occurrence_on(habit, target_date)
        if occurrence is None:
            continue
        completion = completion_map.get(habit.id)
        completion_percentage = (
            float(completion.completion_percentage) if completion else 0.0
        )
        if completion_percentage >= 100:
            completed_count += 1
        scheduled_habits.append(habit)
        completion_lookup[habit.id] = completion_percentage
        occurrence_lookup[habit.id] = occurrence
        completion_entries.append((occurrence, completion_percentage, 1))

    completion_rate = weighted_completion_rate(completion_entries)

    rows = []
    for habit in scheduled_habits:
        occurrence = occurrence_lookup[habit.id]
        completion = completion_map.get(habit.id)
        completion_percentage = completion_lookup.get(habit.id, 0.0)
        raw_value = None
        if completion and completion.raw_value is not None:
            raw_value = float(completion.raw_value)
            if habit.habit_type == Habit.HABIT_QUANTITATIVE:
                raw_value = int(raw_value)
        rows.append(
            {
                "habit": habit,
                "schedule_summary": occurrence.schedule_summary,
                "priority": occurrence.priority,
                "priority_label": occurrence.priority_label,
                "completed": completion_percentage >= 100,
                "completion_percentage": completion_percentage,
                "raw_value": raw_value,
                "tags": habit.get_tags(),
            }
        )

    return {
        "scheduled_count": len(scheduled_habits),
        "completed_count": completed_count,
        "completion_rate": completion_rate,
        "rows": rows,
    }


def daily_average_completion_series(habits, start_date, end_date):
    """Return the canonical per-day completion series for the supplied habits.

    Each value uses the same priority-weighted, partial-aware calculation as
    every headline rate. Missing completion rows contribute 0% on every scored
    date, including today.

    Returns a list of dicts ``{"date", "label", "value"}`` so callers can pick
    either the raw date or formatted label and either float or ``None``.
    """
    habits = list(habits)
    completions = HabitCompletion.objects.filter(
        habit__in=habits,
        date__range=(start_date, end_date),
    )
    completion_map = {
        (completion.habit_id, completion.date): float(completion.completion_percentage or 0)
        for completion in completions
    }
    occurrence_map = {
        (habit.id, occurrence.date): occurrence
        for habit in habits
        for occurrence in iter_scheduled_occurrences(habit, start_date, end_date)
    }

    series = []
    span = (end_date - start_date).days
    for offset in range(span + 1):
        current_day = start_date + timedelta(days=offset)
        daily_entries = []
        for habit in habits:
            occurrence = occurrence_map.get((habit.id, current_day))
            if occurrence is None:
                continue
            completion_key = (habit.id, current_day)
            daily_entries.append(
                (occurrence, completion_map.get(completion_key, 0.0), 1)
            )
        rate = weighted_completion_rate(daily_entries) if daily_entries else None
        series.append(
            {
                "date": current_day,
                "label": current_day.strftime("%b %d"),
                "value": rate,
            }
        )
    return series
