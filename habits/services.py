from datetime import timedelta

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


def get_completion_map(habit, start_date, end_date):
    completions = HabitCompletion.objects.filter(
        habit=habit,
        date__range=(start_date, end_date),
        completed=True,
    )
    return {completion.date: True for completion in completions}


def calculate_streaks(habit, up_to_date=None):
    if up_to_date is None:
        up_to_date = timezone.localdate()

    scheduled_dates = list(iter_scheduled_dates(habit, habit.start_date, up_to_date))
    if not scheduled_dates:
        return 0, 0

    completion_map = get_completion_map(habit, habit.start_date, up_to_date)
    max_streak = 0
    current_run = 0

    for scheduled_date in scheduled_dates:
        if completion_map.get(scheduled_date):
            current_run += 1
            if current_run > max_streak:
                max_streak = current_run
        else:
            current_run = 0

    current_streak = 0
    for scheduled_date in reversed(scheduled_dates):
        if completion_map.get(scheduled_date):
            current_streak += 1
        else:
            break

    return current_streak, max_streak


def completion_stats(habit, start_date, end_date, completion_map=None):
    scheduled_dates = list(iter_scheduled_dates(habit, start_date, end_date))
    scheduled_total = len(scheduled_dates)

    if completion_map is None:
        completion_map = get_completion_map(habit, start_date, end_date)

    completed_total = sum(1 for date in scheduled_dates if completion_map.get(date))
    completion_rate = (
        round((completed_total / scheduled_total) * 100, 1) if scheduled_total else 0.0
    )

    return {
        "scheduled_total": scheduled_total,
        "completed_total": completed_total,
        "completion_rate": completion_rate,
    }


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
