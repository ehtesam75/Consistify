from calendar import monthrange
from dataclasses import dataclass
from datetime import date, timedelta
from math import exp, isfinite, sqrt

from django.db.models import Q
from django.utils import timezone

from .models import (
    DailyRecapCompletion,
    Habit,
    HabitCategory,
    HabitCompletion,
)
# Consistify Score weights. The formula is
#     score = 0.35*Q + 0.20*F + E*(0.30*R + 0.15*M)
# where Q measures how much of the scheduled work was actually completed
# (including partial progress), F measures how often scheduled sessions
# reached exactly 100%, R measures reliability and continuity (recent
# cadence, missed-session avoidance) over the last seven scheduled
# sessions, and M measures trajectory (improvement / decline / stable)
# over the last six scheduled sessions. Q and F together act as the
# "how much work did you do" anchor; R is the reliability term; M is a
# directional modifier that cannot dominate reliability. The component
# weights total 1.0 so the score remains a 0–100 percentage.
#
# ``E`` is the evidence factor (see ``_score_evidence``). Completion can be
# earned immediately, but consistency has to be *demonstrated* through
# repeated scheduled sessions, so R and M only earn their full 45 points
# once the habit has actually repeated. E is a smooth, strictly increasing
# curve of the number of scheduled sessions in the report window, it never
# adds points (it can only hold them back), and it is effectively 1.0 for
# any established habit, so long-term scoring is unchanged.
CONSISTENCY_COMPLETION_QUALITY_WEIGHT = 0.35
CONSISTENCY_FULL_COMPLETION_WEIGHT = 0.20
CONSISTENCY_RHYTHM_WEIGHT = 0.30
CONSISTENCY_RECENT_MOMENTUM_WEIGHT = 0.15
RHYTHM_SESSION_WINDOW = 7
MOMENTUM_SESSION_WINDOW = 6
# Reference point for "meaningful participation". Recent momentum still uses it
# as the denominator of its evidence multiplier (``min(1, latest / 50)``), so it
# must not be removed or repurposed. Consistency rhythm no longer treats it as a
# hard pass/fail line; see ``_rhythm_participation``.
RHYTHM_CONTINUATION_THRESHOLD = 50.0
# Consistency rhythm scores each session with a *soft* participation value
# instead of a binary success flag. Progress at or below the floor counts as no
# participation, progress at or above the ceiling counts as full participation,
# and the band between them transitions smoothly through 0.5 at the 50% mark.
#
# A hard threshold made rhythm discontinuous exactly where most real sessions
# land: 50% scored 0 and 51% scored 1, so a single percentage point could swing
# the reported rhythm by 100 points and the Consistify Score by roughly 30. The
# band keeps the same meaning ("did this session amount to meaningful
# participation?") while making the answer continuous, so no user can gain or
# lose a large amount of score from a trivial difference in progress.
#
# The band is deliberately narrow and centred on 50: sessions at or below 45%
# still earn nothing and sessions at or above 55% still count in full, so every
# history that sat clearly on one side of the old threshold keeps exactly the
# rhythm it had before.
RHYTHM_PARTICIPATION_FLOOR = 45.0
RHYTHM_PARTICIPATION_CEILING = 55.0
MOMENTUM_STABLE_BAND = 5.0
MOMENTUM_FULL_SIGNAL_CHANGE = 50.0
RHYTHM_COVERAGE_WEIGHT = 0.8
RHYTHM_CONTINUITY_WEIGHT = 0.2
RECENT_METRIC_CONFIDENCE_SESSIONS = 3
# Evidence curve for the rhythm/momentum *contribution* to the Consistify
# Score. The raw curve ``1 - exp(-(sessions / SCALE) ** SHAPE)`` is a stretched
# exponential (a Weibull CDF): smooth and gap-free everywhere and strictly
# increasing. ``SHAPE`` below 1 makes the first few repeats worth the most
# evidence and later ones worth progressively less, which mirrors the existing
# ``min(1, k / 3)`` confidence idea inside R and M without its hard kink at
# exactly three sessions.
#
# The curve is then normalised by its own value at ``SCORE_EVIDENCE_FULL_
# SESSIONS`` so that evidence reaches exactly 1 there. That constant is
# ``RHYTHM_SESSION_WINDOW``, because seven sessions is the point where R (and,
# at six, M) already consume every observation they can: past it, more
# in-window sessions tell the recent-signal components nothing new, so there is
# nothing left for the evidence factor to withhold. This matters most for
# low-frequency habits, whose 30-day window holds only a handful of
# occurrences. With these constants a perfectly kept habit earns roughly 56% of
# the R/M points at one session, 74% at two, 84% at three, 95% at five, and all
# of them from seven on.
SCORE_EVIDENCE_SCALE_SESSIONS = 1.6
SCORE_EVIDENCE_SHAPE = 0.65
SCORE_EVIDENCE_FULL_SESSIONS = RHYTHM_SESSION_WINDOW
# Scheduled sessions required before a leaderboard score is trusted in full.
LEADERBOARD_CONFIDENCE_SESSIONS = 30
LEADERBOARD_NEUTRAL_SCORE = 50.0
# Scheduled sessions required before a category score is trusted in full when
# picking the best/weakest category. Categories are scored inside a reporting
# window (30 days by default), so this is deliberately smaller than the
# leaderboard threshold.
CATEGORY_CONFIDENCE_SESSIONS = 10
CATEGORY_NEUTRAL_SCORE = 50.0

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
        # Completion is earned immediately, so it is never evidence-damped.
        "evidence_scaled": False,
        "description": (
            "Average progress across every scheduled session, including "
            "partial completion."
        ),
        "help_text": (
            "How much of the scheduled work you actually completed across "
            "every scheduled session. Partial progress (for example a 60% "
            "session) counts more than a miss, but less than a full 100%. "
            "This is the longest-window signal in the score, so it anchors "
            "the overall completion level."
        ),
    },
    {
        "key": "full_completion",
        "label": "Full completion",
        "metric_key": "full_completion_reliability",
        "weight": CONSISTENCY_FULL_COMPLETION_WEIGHT,
        "evidence_scaled": False,
        "description": (
            "Share of scheduled sessions completed at exactly 100%; acts as a "
            "finishing-quality signal."
        ),
        "help_text": (
            "The percentage of scheduled sessions you finished at 100%. "
            "Partial progress does not count here, so this rewards fully "
            "closing out what you planned. It is a finishing-quality signal "
            "rather than a duplicate of completion quality, because it only "
            "responds to clean closes."
        ),
    },
    {
        "key": "rhythm_stability",
        "label": "Consistency rhythm",
        "metric_key": "streak_stability",
        "weight": CONSISTENCY_RHYTHM_WEIGHT,
        # Consistency has to be demonstrated, so the *points* this component
        # contributes grow with the evidence curve (the reported value does
        # not change).
        "evidence_scaled": True,
        "description": (
            "Reliability and continuity across the last seven scheduled "
            "sessions; rewards meaningful participation and avoids "
            "interruptions."
        ),
        "help_text": (
            "Reliability and continuity across your recent scheduled "
            "sessions. Participation gradually counts more around the 50% "
            "mark, reaching full Rhythm participation at 55%, so a small "
            "difference in progress only makes a small difference here. "
            "It does not measure improvement or decline — that is a "
            "separate signal called Recent Momentum."
        ),
    },
    {
        "key": "recent_momentum",
        "label": "Recent momentum",
        "metric_key": "recent_momentum",
        "weight": CONSISTENCY_RECENT_MOMENTUM_WEIGHT,
        "evidence_scaled": True,
        "description": (
            "Trajectory across the last six scheduled sessions: whether "
            "recent performance is improving, declining, or remaining "
            "stable."
        ),
        "help_text": (
            "Trajectory over the last six scheduled sessions. It compares "
            "recent performance with your recent baseline and rewards "
            "meaningful improvement while filtering out small fluctuations "
            "and sparse data."
            "The value is centred at 50 (neutral), so flat users do not gain or "
            "lose ground from momentum alone."
        ),
    },

)



# ---------------------------------------------------------------------------
# Finalized-analytics cutoff
# ---------------------------------------------------------------------------
# Analytics describe *finished* days. Today is still in progress: a habit that
# reads 0% at 9am may read 100% at 11pm, so letting today into a score, chart,
# rate, ranking, or comparison makes every one of those numbers drift downward
# through the morning and recover by evening. The user would watch their
# Consistify Score fall without doing anything wrong.
#
# The rule is therefore global and centralised here: every historical metric
# ends at ``analytics_end_date`` (yesterday, in the configured local timezone).
# The only exception is the Today page's live progress/completion UI, which is
# explicitly about the unfinished day and reads from ``compute_today_metrics``
# and ``get_pending_habits_for_date`` instead of the analytics helpers below.
#
# The cutoff is derived from ``timezone.localdate()`` on every call, so it rolls
# over on its own at local midnight. No cron job, no scheduled task, and no
# stored snapshot is required for correctness.
ANALYTICS_CUTOFF_NOTE = (
    "Analytics use finalized activity through yesterday. "
    "Today's activity is included after the day ends."
)


def analytics_end_date(today=None):
    """Return the last date whose activity may enter analytics.

    This is the single source of truth for the finalized-day cutoff::

        analytics_end_date = local_today - timedelta(days=1)

    ``today`` defaults to ``timezone.localdate()``, which resolves in the
    active timezone rather than UTC, so the cutoff advances exactly at local
    midnight. Consistify currently runs on a single configured ``TIME_ZONE``
    (``Asia/Dhaka``) shared by every user; because this reads the *active*
    timezone, adding per-user timezones later would move the cutoff with them
    without touching this function.
    """
    if today is None:
        today = timezone.localdate()
    return today - timedelta(days=1)


def clamp_analytics_end(end_date, today=None):
    """Clamp an analytics window end to the finalized-day cutoff.

    Callers may pass ``today`` (or any later date) as their window end; this
    trims it back to yesterday so no analytics path can observe the unfinished
    current day. Windows that already end in the past are returned unchanged.
    """
    cutoff = analytics_end_date(today)
    return min(end_date, cutoff) if end_date > cutoff else end_date


def analytics_window(days, today=None):
    """Return the ``(start, end)`` dates of a ``days``-long finalized window.

    The window ends at ``analytics_end_date`` and keeps its full intended
    length. With today = Aug 10, a 30-day window is Jul 11 - Aug 9 — thirty
    finalized days — rather than the 29-day Jul 12 - Aug 9 that would result
    from measuring the start from today and merely trimming the end.
    """
    end_date = analytics_end_date(today)
    return end_date - timedelta(days=max(1, days) - 1), end_date


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


def has_completed_daily_recap(user, target_date):
    """Return whether the database already records a finished recap.

    This is the account-level source of truth for the prompt, so it is
    consistent across every device, browser, and session.
    """
    return DailyRecapCompletion.objects.filter(
        user=user,
        date=target_date,
    ).exists()


def mark_daily_recap_completed(user, target_date):
    """Persist that ``user`` finished the recap for ``target_date``.

    Idempotent, so repeated submissions from different devices or duplicate
    requests do not raise on the unique constraint.
    """
    completion, _ = DailyRecapCompletion.objects.get_or_create(
        user=user,
        date=target_date,
    )
    return completion


def should_show_daily_recap(user, today=None):
    """Resolve the prompt from persisted state only.

    Returns ``(should_show, target_date, pending_habits)``. The decision uses
    the user's stored recap record and their stored completions, never session,
    browser, device, or login state, so the answer is identical on every
    device, browser, session, and repeated login.

    ``last_login`` is deliberately not consulted. Logging in overwrites it, so
    it reports "already seen today" to every later request in the same day and
    to every other device that logs in afterwards. Only the persisted recap
    record can answer this correctly for the whole account.
    """
    if today is None:
        today = timezone.localdate()

    target_date = daily_recap_target_date(today)

    # The persisted recap record is the account-level source of truth. If the
    # user already finished this date's recap anywhere, it stays hidden here.

    if has_completed_daily_recap(user, target_date):
        return False, target_date, []

    pending = get_pending_habits_for_date(user, target_date)
    if not pending:
        return False, target_date, []

    return True, target_date, pending


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
    habit_type: str = Habit.HABIT_BINARY
    target_value: object = None
    unit: str = ""

    @property
    def priority_label(self):
        return PRIORITY_LABELS.get(self.priority, self.priority.title())

    @property
    def is_quantitative(self):
        return self.habit_type == Habit.HABIT_QUANTITATIVE

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
        habit_type=habit.habit_type,
        target_value=habit.target_value,
        unit=habit.unit or "",
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
            habit_type=version.habit_type,
            target_value=version.target_value,
            unit=version.unit or "",
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


def _archive_cutoff_date(habit):
    """Return the last date an archived habit is still tracked, or ``None``.

    Archiving takes effect on ``archive_effective_date`` (tomorrow when the
    user archives), so the final tracked date is the day before it. History,
    reports, and Consistify Scores up to that date are preserved.
    """
    effective_date = getattr(habit, "archive_effective_date", None)
    if effective_date is None:
        return None
    return effective_date - timedelta(days=1)


def iter_scheduled_occurrences(habit, start_date, end_date):
    """Yield scheduled sessions with their occurrence-time configuration."""
    archive_cutoff = _archive_cutoff_date(habit)
    if archive_cutoff is not None:
        end_date = min(end_date, archive_cutoff)

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
            # Raw values stay untyped here on purpose. The habit's *current*
            # habit_type must never decide how a historical value is read, or
            # editing the type would silently reinterpret history logged under
            # the previous plan. Formatting is resolved later from the plan
            # version that was actually in effect on each date.
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
    """Return whether progress clears the legacy binary success threshold.

    Kept for callers that need the original pass/fail reading of a session.
    Consistency rhythm no longer uses it — it scores participation on the
    smooth band defined by ``_rhythm_participation`` — but the threshold itself
    is still meaningful elsewhere (recent momentum's evidence multiplier).
    """
    return _clamped_percentage(percentage_value) > RHYTHM_CONTINUATION_THRESHOLD


def _rhythm_participation(percentage_value):
    """Return how much a session counts as meaningful participation, 0..1.

    This replaces the binary "did this session beat 50%?" test used by
    consistency rhythm. Progress at or below ``RHYTHM_PARTICIPATION_FLOOR``
    counts as no participation, progress at or above
    ``RHYTHM_PARTICIPATION_CEILING`` counts in full, and the band between them
    ramps smoothly through 0.5 at the midpoint::

        x = clamp((progress - FLOOR) / (CEILING - FLOOR), 0, 1)
        participation = 3x^2 - 2x^3

    The ramp is the standard smoothstep curve, so participation is continuous
    *and* flattens out at both ends of the band. That matters: a linear ramp
    would still have a visible corner at 45% and 55%, and the whole point of
    the band is that no single percentage point of progress should ever move
    the score sharply.

    With the default 45/55 band the curve reads:
        <=45% -> 0.00    46% -> 0.03    49% -> 0.35    50% -> 0.50
         51% -> 0.65     54% -> 0.97    >=55% -> 1.00

    Because the band is closed at both ends, every session that sat clearly on
    one side of the old 50% threshold produces exactly the participation the
    old boolean produced (0 below 45, 1 above 55). Histories that never enter
    the band therefore score exactly the rhythm they scored before.
    """
    percentage = _clamped_percentage(percentage_value)
    span = RHYTHM_PARTICIPATION_CEILING - RHYTHM_PARTICIPATION_FLOOR
    if span <= 0:
        # Degenerate configuration: fall back to the binary reading rather
        # than dividing by zero.
        return 1.0 if percentage > RHYTHM_PARTICIPATION_FLOOR else 0.0
    ramp = (percentage - RHYTHM_PARTICIPATION_FLOOR) / span
    ramp = max(0.0, min(1.0, ramp))
    return ramp * ramp * (3.0 - 2.0 * ramp)


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
    """Return a reliability/continuity score for the most recent scheduled sessions.

    Consistency rhythm measures **reliability and continuity** — how
    consistently the user keeps showing up for recent scheduled sessions and
    avoids interruptions. It is a *level* signal, not a *trend* signal:
    improvement and decline are reported separately by ``_recent_momentum``.

    Definition:
        * Look at the last ``RHYTHM_SESSION_WINDOW`` scheduled sessions.
        * Each session is scored by ``_rhythm_participation`` on a smooth
          0..1 band: at or below 45% it counts as no participation, at or
          above 55% it counts in full, and it passes through 0.5 at the 50%
          mark. An unlogged session is 0% progress and so contributes 0.
          This replaced a hard ``> 50%`` test, which made a single
          percentage point of progress worth up to 100 rhythm points.
        * ``coverage`` is the average participation across recent sessions.
        * ``continuity`` is the average of ``min(previous, current)`` across
          consecutive sessions. A transition is only as strong as its weaker
          end, which is the smooth reading of "were both of these sessions
          kept?" — with 0/1 participation it reduces to exactly the old
          "previous *and* current succeeded" count. With one session there
          are no transitions, so ``continuity`` is defined as ``coverage``
          (a single observation cannot make a continuity claim beyond its
          own coverage).
        * ``raw_rhythm = 0.8 * coverage + 0.2 * continuity`` (so a single
          session has ``raw_rhythm = coverage``).
        * ``confidence = min(1, session_count / 3)`` and the result is
          shrunk toward 50% by ``confidence`` so one or two sessions cannot
          claim a perfect rhythm.
        * A window in which every relevant session is **completely
          untouched** (0% progress, logged or not) reports ``0`` directly,
          whatever the session count. There is no rhythm to be confident or
          unconfident about, and shrinking toward the neutral 50% would hand
          out points for a record the user never earned. This zero-activity
          floor is deliberately narrow: any real progress at all, including
          progress that earns little or no participation, goes through the
          normal coverage/continuity/confidence pipeline unchanged.
    """
    recent_dates = scheduled_dates[-RHYTHM_SESSION_WINDOW:]
    if not recent_dates:
        return 0.0

    completion_values = [
        _clamped_percentage(completion_map.get(scheduled_date, 0))
        for scheduled_date in recent_dates
    ]
    if not any(completion_values):
        # Zero activity. Every relevant session is untouched, so there is no
        # evidence to be unconfident *about*: confidence may damp an unproven
        # claim, but it must never *credit* an untouched record. Note this
        # tests the raw progress values, not participation — a user with
        # genuine partial progress (10%, 40%, 50%) has done real work and must
        # keep flowing through the normal calculation below, even when that
        # progress earns little or no participation.
        return 0.0

    participation_values = [
        _rhythm_participation(value) for value in completion_values
    ]
    session_count = len(participation_values)
    coverage = sum(participation_values) / session_count

    if session_count == 1:
        # With one session there are no transitions; continuity is defined
        # as equal to coverage so ``raw_rhythm`` reduces to ``coverage``.
        continuity = coverage

    else:
        # ``min`` is the smooth reading of the old "previous *and* current
        # succeeded" rule: a run of sessions is only as continuous as its
        # weakest link. With binary 0/1 participation this is exactly the
        # old successful-pair count, so histories that never enter the
        # 45-55 band keep the rhythm they had before. It also leaves a
        # perfectly steady history alone, because equal participation
        # values make ``continuity == coverage`` and therefore
        # ``raw_rhythm == coverage`` — a consistent user is never penalised
        # twice for their own consistency.
        continuity = sum(
            min(previous, current)
            for previous, current in zip(
                participation_values, participation_values[1:]
            )
        ) / (session_count - 1)

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
    """Return a trajectory score for the most recent scheduled sessions.

    Recent momentum measures **trajectory** — whether recent performance is
    improving, declining, or remaining stable compared with the user's
    recent baseline. It is a *trend* signal, not a *reliability* signal:
    regularity and missed-session avoidance are reported separately by
    ``_consistency_rhythm``. Trajectory is centred at 50 (neutral) and
    symmetric for improvement vs. decline, so a flat user does not gain or
    lose ground from momentum alone.

    Definition:
        * Look at the last ``MOMENTUM_SESSION_WINDOW`` scheduled sessions.
        * Each pairwise change is mapped through ``_momentum_change_signal``
          with a ``MOMENTUM_STABLE_BAND`` (5pp) deadband: changes inside
          the deadband are noise (signal = 0).
        * Larger changes are normalised so a ``MOMENTUM_FULL_SIGNAL_CHANGE``
          (50pp) change produces a full ±1 signal.
        * Newer transitions receive larger ordinal weights, so the trend is
          dominated by the most recent comparison — this keeps daily and
          weekly cadences comparable and prevents calendar gaps from
          affecting the result.
        * ``confidence = min(1, len / 3)`` shrinks the trend toward 50%
          when fewer than three observations are available.
        * The latest session's progress supplies an *evidence* multiplier
          (``min(1, latest / 50)``) that prevents an old high result from
          propping up sustained low recent performance. This damping is
          applied only to the trajectory signal — the *level* itself is
          reported separately by completion quality and consistency
          rhythm, so evidence is not a reliability measurement.
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


def _raw_score_evidence(sessions):
    """Return the un-normalised stretched-exponential evidence curve."""
    growth = (sessions / SCORE_EVIDENCE_SCALE_SESSIONS) ** SCORE_EVIDENCE_SHAPE
    return 1.0 - exp(-growth)


# Value of the raw curve at full evidence, so ``_score_evidence`` can normalise
# to exactly 1.0 there. Computed once at import; the constants never change at
# runtime.
_FULL_SCORE_EVIDENCE = _raw_score_evidence(SCORE_EVIDENCE_FULL_SESSIONS)


def _score_evidence(session_count):
    """Return how much of the rhythm/momentum points a record has earned.

    Completion can be earned immediately: one kept session really is one
    session's worth of work, so completion quality and full completion are
    reported in full straight away. Consistency is a different claim. Rhythm
    and momentum both describe *repeated* behaviour — cadence, continuity, and
    trajectory — and a habit with a single scheduled session has not repeated
    anything yet, so it cannot have demonstrated either one. Their measured
    values stay exactly as ``_consistency_rhythm`` and ``_recent_momentum``
    define them; only the number of *points* they contribute to the Consistify
    Score is held back until the repetition exists.

    The factor is a stretched exponential (Weibull CDF), normalised so that it
    reaches exactly 1 at ``SCORE_EVIDENCE_FULL_SESSIONS``::

        raw(k)   = 1 - exp(-(k / SCALE) ** SHAPE)
        evidence = min(1, raw(sessions) / raw(FULL_SESSIONS))

    Properties that make it fit the existing design:
        * ``evidence(0) = 0`` and it grows continuously and strictly with every
          extra scheduled session up to full evidence, so there is no step at
          any session count and no threshold to game. Adding one more session
          always helps a little.
        * ``SHAPE < 1`` front-loads the curve: the first repeats are worth the
          most evidence and later ones progressively less. That is the same
          intuition as the ``min(1, k / 3)`` confidence already used inside R
          and M, but smooth instead of kinked at exactly three sessions, and it
          keeps working past three.
        * Full evidence is ``RHYTHM_SESSION_WINDOW`` sessions, which is exactly
          where R stops taking in new observations (M stops at six). Beyond that
          point, extra in-window sessions cannot tell R or M anything further,
          so there is nothing left to withhold and evidence stays at 1. This
          keeps established *low-frequency* habits whole: a year-old weekly
          habit whose 30-day window holds five sessions is judged on the same
          scale the recent-signal components actually use, instead of being
          discounted for a calendar window it cannot fill.
        * It is a multiplier in ``[0, 1]``, so it can only ever hold points
          back, never create them. A completely untouched window still scores
          ``0`` rhythm and ``0`` momentum, so the zero-activity floor is
          untouched: ``0 * evidence`` is still ``0``.

    Evidence is counted strictly inside the reporting window, from the same
    ``scheduled_total`` the components are measured over. Older sessions from
    outside the period are deliberately not consulted: R and M cannot see them
    either, so letting them raise the evidence factor would credit a value with
    observations that never entered its calculation.
    """
    try:
        sessions = float(session_count)
    except (TypeError, ValueError):
        return 0.0
    if not isfinite(sessions) or sessions <= 0:
        return 0.0
    return max(0.0, min(1.0, _raw_score_evidence(sessions) / _FULL_SCORE_EVIDENCE))


def _consistency_score(
    completion_quality,
    full_completion_ratio,
    consistency_rhythm,
    recent_momentum,
    evidence=1.0,
):
    """Return ``0.35*Q + 0.20*F + E*(0.30*R + 0.15*M)`` as a 0-100 score.

    ``evidence`` scales only the rhythm and momentum *contribution*; see
    ``_score_evidence``. It defaults to 1.0 so the formula reduces to the
    plain weighted sum for a fully evidenced record.
    """
    earned_completion = (
        completion_quality * CONSISTENCY_COMPLETION_QUALITY_WEIGHT
        + full_completion_ratio * CONSISTENCY_FULL_COMPLETION_WEIGHT
    )
    demonstrated_consistency = (
        consistency_rhythm * CONSISTENCY_RHYTHM_WEIGHT
        + recent_momentum * CONSISTENCY_RECENT_MOMENTUM_WEIGHT
    )
    score = (earned_completion + evidence * demonstrated_consistency) * 100
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


def _habit_consistency_metrics(habit, start_date, end_date, precomputed):
    """Return the per-habit metrics dict for a single habit.

    Callers that already iterated ``habits`` to build other aggregates can pass
    the previously-computed ``per_habit`` list (in the same shape
    ``compute_user_metrics`` returns) so this window's metrics reuse the same
    database reads instead of recomputing the completion maps.
    """
    if precomputed is not None:
        for row in precomputed:
            if row["habit"].pk == habit.pk:
                return row["metrics"]
    return habit_performance_metrics(habit, start_date, end_date)


def _aggregate_consistency_snapshot(
    habits,
    start_date,
    end_date,
    precomputed_metrics=None,
):
    weighted_score = 0.0
    total_weight = 0.0
    scheduled_total = 0
    completed_total = 0
    component_totals = {component["key"]: 0.0 for component in CONSISTENCY_SCORE_COMPONENTS}
    # Points are accumulated separately from values because the rhythm and
    # momentum components contribute evidence-scaled points while still
    # reporting their measured value. Tracking both keeps the breakdown an
    # exact decomposition of the score it explains.
    component_point_totals = {
        component["key"]: 0.0 for component in CONSISTENCY_SCORE_COMPONENTS
    }

    for habit in habits:
        metrics = _habit_consistency_metrics(
            habit, start_date, end_date, precomputed_metrics
        )
        if metrics["scheduled_total"] == 0:
            continue

        weight = _habit_consistency_weight(
            habit,
            metrics["scheduled_total"],
            metrics["priority_weight_total"],
        )
        if weight == 0:
            continue

        evidence = metrics.get("score_evidence")
        if evidence is None:
            evidence = _score_evidence(metrics["scheduled_total"])

        total_weight += weight
        scheduled_total += metrics["scheduled_total"]
        completed_total += metrics["completed_total"]
        weighted_score += metrics["consistency_score"] * weight
        for component in CONSISTENCY_SCORE_COMPONENTS:
            value = metrics[component["metric_key"]]
            component_totals[component["key"]] += value * weight
            points = value * component["weight"]
            if component.get("evidence_scaled"):
                points *= evidence
            component_point_totals[component["key"]] += points * weight

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
        points = component_point_totals[component["key"]] / total_weight
        components[component["key"]] = {
            "value": round(value, 1),
            "points": round(points, 1),
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
        config = occurrence.config
        completion = completion_map.get(habit.id)
        completion_percentage = (
            float(completion.completion_percentage) if completion else 0.0
        )
        raw_value = None
        if completion and completion.raw_value is not None:
            raw_value = float(completion.raw_value)
            if config.is_quantitative:
                raw_value = int(raw_value)

        if completion is None or completion_percentage < 100:
            pending.append(
                {
                    "habit": habit,
                    "config": config,
                    "habit_type": config.habit_type,
                    "target_value": config.target_value,
                    "unit": config.unit,
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
    precomputed_metrics=None,
):
    # ``precomputed_metrics`` describes the current window only. The previous
    # window may cover a different date range, so its metrics must always be
    # recomputed from scratch.
    current = _aggregate_consistency_snapshot(
        habits, start_date, end_date, precomputed_metrics
    )
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
                "help_text": component["help_text"],
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
    precomputed_metrics=None,
):
    current_rows = []
    total_weight = 0.0

    for habit in habits:
        metrics = _habit_consistency_metrics(
            habit, start_date, end_date, precomputed_metrics
        )
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


def category_ranking_score(
    consistency_score,
    scheduled_total,
    confidence_sessions=CATEGORY_CONFIDENCE_SESSIONS,
):
    """Return an evidence-adjusted category score used only for ranking.

    A category's displayed statistics stay exactly as measured. This value
    decides which category is crowned *Best* or flagged *Weakest*, and it
    exists because a single perfect session produces the same raw score as a
    long, reliable history, and a single bad session produces the same raw
    score as a genuinely neglected category.

    With ``k`` scheduled sessions, confidence is ``min(1, k / 10)`` and the
    score is pulled toward a neutral 50 by whatever confidence is missing.
    The shrink is two-sided here on purpose — unlike the leaderboard, this
    number never represents a user's standing against other people, and both
    superlatives must be evidence-gated: a thin high score must not be crowned
    best, and a thin low score must not be blamed as weakest. Confidence grows
    continuously with each session, so nothing jumps at an arbitrary cutoff.
    """
    if scheduled_total <= 0:
        return None
    try:
        confidence = min(1.0, float(scheduled_total) / float(confidence_sessions))
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    shrunk = CATEGORY_NEUTRAL_SCORE + confidence * (
        consistency_score - CATEGORY_NEUTRAL_SCORE
    )
    return round(shrunk, 1)


def build_category_analytics(habits, start_date, end_date):
    # Category analytics attribute finalized sessions to categories, so the
    # window is trimmed to yesterday before any occurrence is counted. This
    # also keeps ``scheduled_total`` — the evidence count behind
    # ``category_ranking_score`` — free of today's session, so a category
    # cannot be crowned best or blamed weakest on the strength of a day the
    # user has not finished yet.
    end_date = clamp_analytics_end(end_date)
    summaries = []
    best_category = None
    weakest_category = None

    categories = list(HabitCategory.objects.all())

    category_metrics = {category.id: [] for category in categories}

    for habit in habits:
        occurrences = list(
            iter_finalized_occurrences(habit, start_date, end_date)
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
            "ranking_score": category_ranking_score(
                consistency_score,
                total_scheduled,
            ),
            "is_best": False,
            "is_weakest": False,
        }
        summaries.append(summary)

        if total_scheduled == 0:
            continue
        # Superlatives are decided on the evidence-adjusted ranking score so a
        # single lucky (or unlucky) session cannot outrank a longer, more
        # reliable history. The displayed ``consistency_score`` and
        # ``completion_rate`` above stay exactly as measured.
        ranking_score = summary["ranking_score"]
        if (
            best_category is None
            or ranking_score > best_category["ranking_score"]
        ):
            best_category = summary
        if (
            weakest_category is None
            or ranking_score < weakest_category["ranking_score"]
        ):
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
            "average_value": 0.0,
            "average_value_unit": "",
            "average_value_is_quantitative": False,
            "average_value_spans_mixed_plans": False,
            "completion_quality": 0.0,
            "full_completion_reliability": 0.0,
            "recent_momentum": 0.0,
            "score_evidence": 0.0,
        }

    streaks = _streak_metrics(scheduled_dates, completion_map)
    completed_total = streaks["completed_total"]
    missed_total = scheduled_total - completed_total
    total_completion = 0.0
    weighted_completion_total = 0.0
    priority_weight_total = 0.0
    # Raw values are grouped by the plan version in effect when they were
    # logged. The habit's *current* habit_type must never decide how a
    # historical value is read, and values recorded under different units are
    # never averaged together, because "30 minutes" and "1 hour" are not
    # comparable numbers.
    value_groups = {}

    for occurrence in occurrences:
        completion = _clamped_percentage(completion_map.get(occurrence.date, 0))
        total_completion += completion
        weighted_completion_total += completion * occurrence.priority_weight
        priority_weight_total += occurrence.priority_weight

        config = occurrence.config
        group = value_groups.setdefault(
            (config.habit_type, config.unit or ""),
            {"total": 0.0, "count": 0, "effective_from": config.effective_from},
        )
        group["total"] += value_map.get(occurrence.date, 0) or 0
        group["count"] += 1
        if config.effective_from > group["effective_from"]:
            group["effective_from"] = config.effective_from

    completion_rate = weighted_completion_rate_from_totals(
        weighted_completion_total,
        priority_weight_total,
    )

    # Report the average for the most recently effective plan in this window so
    # the number always matches the unit it is labelled with.
    average_value = 0.0
    average_value_unit = ""
    average_value_is_quantitative = False
    average_value_spans_mixed_plans = len(value_groups) > 1
    if value_groups:
        (latest_habit_type, latest_unit), latest_group = max(
            value_groups.items(),
            key=lambda item: item[1]["effective_from"],
        )
        average_value_is_quantitative = (
            latest_habit_type == Habit.HABIT_QUANTITATIVE
        )
        average_value_unit = latest_unit
        raw_average = latest_group["total"] / latest_group["count"]
        average_value = (
            round(raw_average)
            if average_value_is_quantitative
            else round(raw_average, 2)
        )

    completion_quality = (total_completion / scheduled_total) / 100
    full_completion_ratio = completed_total / scheduled_total
    consistency_rhythm = _consistency_rhythm(scheduled_dates, completion_map)
    recent_momentum = _recent_momentum(scheduled_dates, completion_map)
    # Rhythm and momentum are measured and reported in full; only how much of
    # their points reach the Consistify Score depends on how many scheduled
    # sessions this window actually contains.
    evidence = _score_evidence(scheduled_total)
    consistency_score = _consistency_score(
        completion_quality=completion_quality,
        full_completion_ratio=full_completion_ratio,
        consistency_rhythm=consistency_rhythm,
        recent_momentum=recent_momentum,
        evidence=evidence,
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
        "average_value": average_value,
        "average_value_unit": average_value_unit,
        "average_value_is_quantitative": average_value_is_quantitative,
        "average_value_spans_mixed_plans": average_value_spans_mixed_plans,
        "completion_quality": round(completion_quality * 100, 1),
        "full_completion_reliability": round(full_completion_ratio * 100, 1),
        "recent_momentum": round(recent_momentum * 100, 1),
        "score_evidence": evidence,
    }


def iter_finalized_occurrences(habit, start_date, end_date):
    """Yield scheduled sessions inside the finalized-analytics window.

    This is the analytics-side gate in front of
    ``iter_scheduled_occurrences``: the requested ``end_date`` is trimmed to
    ``analytics_end_date`` so today's session can never enter a scored window.
    Because every historical metric counts sessions through this helper,
    ``scheduled_total``, the Rhythm and Momentum session lists, ``E(k)``, the
    aggregation weights, and the leaderboard/category evidence counts all move
    together and stay consistent with one another.

    The Today page deliberately does *not* come through here; it calls
    ``scheduled_occurrence_on`` so its live UI keeps seeing the current day.
    """
    yield from iter_scheduled_occurrences(
        habit,
        start_date,
        clamp_analytics_end(end_date),
    )


def habit_performance_metrics(
    habit,
    start_date,
    end_date,
    completion_map=None,
    value_map=None,
):
    # Analytics end at yesterday. Clamping the window here rather than in each
    # metric means Q, F, R, M, E, the completion rate, the streaks, and every
    # aggregate built on top of them are all measured over the same finalized
    # set of sessions. Today's completion rows may still be present in a
    # caller-supplied ``completion_map``; they are simply never looked up,
    # because no occurrence carries today's date.
    end_date = clamp_analytics_end(end_date)
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
    # Streaks are historical statistics, so they end at the analytics cutoff
    # inside ``habit_performance_metrics``. A habit completed today therefore
    # extends its streak tomorrow, which keeps the number stable instead of
    # letting it appear and disappear during the day.
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
        # ``completion_rate`` is the single canonical priority-weighted average
        # progress figure. There is deliberately no ``average_completion``
        # alias, because two names for one calculation read as two different
        # metrics in the UI.
        "completion_rate": metrics["completion_rate"],
        "average_value": metrics["average_value"],
        "average_value_unit": metrics["average_value_unit"],
        "average_value_is_quantitative": metrics["average_value_is_quantitative"],
        "average_value_spans_mixed_plans": metrics[
            "average_value_spans_mixed_plans"
        ],
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

    # Weekly snapshots are anchored on the finalized cutoff rather than on
    # today, so the newest bucket is the week containing yesterday and its
    # label stops at yesterday. Anchoring (instead of merely trimming the last
    # period) keeps the requested number of periods intact and stops the label
    # from advertising a date whose data is deliberately excluded.
    report_end = clamp_analytics_end(today)
    current_week_start = report_end - timedelta(days=report_end.weekday())

    reports = []
    for week_index in reversed(range(weeks)):
        period_start = current_week_start - timedelta(days=week_index * 7)
        period_end = min(period_start + timedelta(days=6), report_end)
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

    # As with the weekly reports, months are anchored on the finalized cutoff
    # so a partially-elapsed month is only ever reported through yesterday.
    report_end = clamp_analytics_end(today)

    reports = []
    for months_back in reversed(range(months)):
        year, month = _shift_month(report_end, months_back)
        first_day = date(year, month, 1)
        last_day = date(year, month, monthrange(year, month)[1])
        if report_end < last_day:
            last_day = report_end

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

    archive_cutoff = _archive_cutoff_date(habit)
    if archive_cutoff is not None and from_date > archive_cutoff:
        return None


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


def leaderboard_ranking_score(
    consistency_score,
    scheduled_total,
    confidence_sessions=LEADERBOARD_CONFIDENCE_SESSIONS,
):
    """Return an evidence-adjusted Consistify Score for ranking users.

    A raw Consistify Score says nothing about how much evidence produced it.
    Three perfect days and two perfect years both read as 100, so ranking on
    the raw score lets a brand new account outrank sustained long-term
    consistency.

    With ``k`` scheduled sessions, confidence is ``min(1, k / 30)`` and an
    unproven score is pulled toward a neutral 50 by whatever confidence is
    missing. A full window of evidence is therefore reported unchanged, while a
    thin record cannot claim a high rank until it has been earned.

    The adjustment is deliberately **one-sided**: it may only ever lower a
    score, never raise it. A two-sided shrink toward 50 also lifts weak
    low-evidence records upward, so an untouched newcomer who earned almost no
    Consistify Score would rank near 50 and outrank established users with a
    genuine 40-45. Ranking on ``min(score, shrunk)`` keeps the damping for
    unproven highs while guaranteeing nobody is ranked above what they scored.

    The displayed Consistify Score is deliberately left untouched; this value
    only decides ranking order.
    """
    if scheduled_total <= 0:
        return 0.0
    try:
        confidence = min(1.0, float(scheduled_total) / float(confidence_sessions))
    except (TypeError, ValueError, ZeroDivisionError):
        return 0.0
    shrunk = LEADERBOARD_NEUTRAL_SCORE + confidence * (
        consistency_score - LEADERBOARD_NEUTRAL_SCORE
    )
    return round(min(consistency_score, shrunk), 1)



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
        config = occurrence.config
        completion = completion_map.get(habit.id)
        completion_percentage = completion_lookup.get(habit.id, 0.0)
        raw_value = None
        if completion and completion.raw_value is not None:
            raw_value = float(completion.raw_value)
            if config.is_quantitative:
                raw_value = int(raw_value)
        rows.append(
            {
                "habit": habit,
                "config": config,
                "habit_type": config.habit_type,
                "target_value": config.target_value,
                "unit": config.unit,
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
    date.

    The series ends at ``analytics_end_date``, so no chart plots a point (or
    prints an axis label) for today. A partially-finished day would otherwise
    render as a misleading dip at the right-hand edge of every trend line.

    Returns a list of dicts ``{"date", "label", "value"}`` so callers can pick
    either the raw date or formatted label and either float or ``None``.
    """
    habits = list(habits)
    end_date = clamp_analytics_end(end_date)
    if end_date < start_date:
        return []

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
