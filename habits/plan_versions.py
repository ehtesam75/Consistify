from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

from django.core.exceptions import ValidationError

from django.db import DEFAULT_DB_ALIAS, transaction
from django.utils import timezone
from django.utils.dateparse import parse_date

from .models import Habit, HabitCategory, HabitPlanVersion


PLAN_MIRROR_FIELDS = (
    "habit_type",
    "target_value",
    "unit",
    "schedule_type",
    "start_date",
    "interval_days",
    "weekly_interval",
    "days_of_week",
    "priority",
)


def _database_for(habit):
    return habit._state.db or DEFAULT_DB_ALIAS


def _local_creation_date(habit):
    if habit.created_at is None:
        return timezone.localdate()
    if timezone.is_aware(habit.created_at):
        return timezone.localtime(habit.created_at).date()
    return habit.created_at.date()


def _plan_defaults(habit):
    return {
        "schedule_anchor": habit.start_date,
        "habit_type": habit.habit_type,
        "target_value": habit.target_value,
        "unit": habit.unit or "",
        "schedule_type": habit.schedule_type,
        "interval_days": max(1, habit.interval_days or 1),
        "weekly_interval": max(1, habit.weekly_interval or 1),
        "days_of_week": habit.days_of_week or "",
        "priority": habit.priority,
    }


def _normalized_target_value(value):
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ValidationError(
            {"target_value": "Enter a valid target value."}
        ) from None



def _ensure_initial_for_locked(habit):
    existing = habit.plan_versions.order_by("effective_from", "id").first()
    if existing is not None:
        return existing

    database = _database_for(habit)
    version = HabitPlanVersion.objects.using(database).create(
        habit=habit,
        effective_from=min(habit.start_date, _local_creation_date(habit)),
        **_plan_defaults(habit),
    )
    version.categories.set(habit.categories.all())
    return version


def ensure_initial_plan_version(habit):
    """Create the baseline plan for an existing habit, if it has none."""
    if habit.pk is None:
        raise ValueError("A habit must be saved before its plan can be versioned.")

    database = _database_for(habit)
    with transaction.atomic(using=database):
        locked = Habit.objects.using(database).select_for_update().get(pk=habit.pk)
        version = _ensure_initial_for_locked(locked)

    prefetched = getattr(habit, "_prefetched_objects_cache", None)
    if prefetched is not None:
        prefetched.pop("plan_versions", None)
    return version


def _normalized_date(value, field_name):
    if isinstance(value, date):
        return value
    parsed = parse_date(value) if isinstance(value, str) else None
    if parsed is None:
        raise ValidationError({field_name: "Enter a valid date."})
    return parsed


def _normalized_days(value):
    raw_days = value.split(",") if isinstance(value, str) else value or []
    try:
        days = sorted({int(day) for day in raw_days if str(day).strip()})
    except (TypeError, ValueError):
        raise ValidationError({"days_of_week": "Choose valid weekdays."}) from None
    if any(day < 0 or day > 6 for day in days):
        raise ValidationError({"days_of_week": "Choose valid weekdays."})
    return ",".join(str(day) for day in days)


def _category_ids(categories, database):
    if categories is None:
        return None
    if hasattr(categories, "values_list"):
        category_ids = list(categories.values_list("pk", flat=True))
    else:
        try:
            category_ids = [
                category.pk if isinstance(category, HabitCategory) else int(category)
                for category in categories
            ]
        except (TypeError, ValueError):
            raise ValidationError({"categories": "Choose valid categories."}) from None
    category_ids = list(dict.fromkeys(category_ids))
    existing_count = HabitCategory.objects.using(database).filter(
        pk__in=category_ids
    ).count()
    if existing_count != len(category_ids):
        raise ValidationError({"categories": "Choose valid categories."})
    return category_ids


def schedule_habit_plan_edit(
    habit,
    *,
    habit_type=None,
    target_value=None,
    unit=None,
    schedule_type=None,
    start_date=None,
    interval_days=None,
    weekly_interval=None,
    days_of_week=None,
    priority=None,
    categories=None,
    today=None,
):
    """Schedule configuration changes for tomorrow and mirror the latest plan.

    A second edit on the same day updates the existing pending version. The
    active and historical versions remain untouched.
    """
    if habit.pk is None:
        raise ValueError("A habit must be saved before its plan can be edited.")
    if today is None:
        today = timezone.localdate()
    effective_from = today + timedelta(days=1)
    database = _database_for(habit)

    with transaction.atomic(using=database):
        locked = Habit.objects.using(database).select_for_update().get(pk=habit.pk)
        _ensure_initial_for_locked(locked)

        values = _plan_defaults(locked)
        if habit_type is not None:
            values["habit_type"] = habit_type
        if target_value is not None:
            values["target_value"] = _normalized_target_value(target_value)
        if unit is not None:
            values["unit"] = unit or ""
        if schedule_type is not None:
            values["schedule_type"] = schedule_type
        if start_date is not None:
            values["schedule_anchor"] = _normalized_date(start_date, "start_date")
        try:
            if interval_days is not None:
                values["interval_days"] = int(interval_days)
            if weekly_interval is not None:
                values["weekly_interval"] = int(weekly_interval)
        except (TypeError, ValueError):
            raise ValidationError("Schedule intervals must be whole numbers.") from None
        if days_of_week is not None:
            values["days_of_week"] = _normalized_days(days_of_week)
        if priority is not None:
            values["priority"] = priority

        valid_schedules = {choice for choice, _label in Habit.SCHEDULE_CHOICES}
        valid_priorities = {choice for choice, _label in Habit.PRIORITY_CHOICES}
        if values["schedule_type"] not in valid_schedules:
            raise ValidationError({"schedule_type": "Choose a valid schedule."})
        if values["priority"] not in valid_priorities:
            raise ValidationError({"priority": "Choose a valid priority."})
        if values["interval_days"] < 1:
            raise ValidationError({"interval_days": "Enter at least 1 day."})
        if values["weekly_interval"] < 1:
            raise ValidationError({"weekly_interval": "Enter at least 1 week."})
        if values["schedule_type"] == Habit.SCHEDULE_DAYS:
            if not values["days_of_week"]:
                raise ValidationError({"days_of_week": "Choose at least one day."})
        else:
            values["days_of_week"] = ""

        category_ids = _category_ids(categories, database)
        if category_ids is None:
            category_ids = list(locked.categories.values_list("pk", flat=True))
        if not 1 <= len(category_ids) <= 3:
            raise ValidationError(
                {"categories": "Choose between one and three categories."}
            )

        version, _created = HabitPlanVersion.objects.using(database).update_or_create(
            habit=locked,
            effective_from=effective_from,
            defaults=values,
        )
        version.categories.set(category_ids)

        locked.habit_type = values["habit_type"]
        locked.target_value = values["target_value"]
        locked.unit = values["unit"]
        locked.schedule_type = values["schedule_type"]
        locked.start_date = values["schedule_anchor"]
        locked.interval_days = values["interval_days"]
        locked.weekly_interval = values["weekly_interval"]
        locked.days_of_week = values["days_of_week"]
        locked.priority = values["priority"]
        locked.save(update_fields=[*PLAN_MIRROR_FIELDS, "updated_at"])
        locked.categories.set(category_ids)

    habit.refresh_from_db(using=database)
    habit._prefetched_objects_cache = {}
    return version
