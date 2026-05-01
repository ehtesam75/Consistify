from decimal import Decimal

from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils import timezone


class Habit(models.Model):
    HABIT_BINARY = "binary"
    HABIT_PARTIAL = "partial"
    HABIT_QUANTITATIVE = "quantitative"
    HABIT_TYPE_CHOICES = [
        (HABIT_BINARY, "Binary (Done / Not Done)"),
        (HABIT_PARTIAL, "Partial (0-100%)"),
        (HABIT_QUANTITATIVE, "Quantitative (Target value)"),
    ]

    SCHEDULE_DAILY = "daily"
    SCHEDULE_WEEKLY = "weekly"
    SCHEDULE_DAYS = "days"
    SCHEDULE_INTERVAL = "interval"

    SCHEDULE_CHOICES = [
        (SCHEDULE_DAILY, "Daily"),
        (SCHEDULE_WEEKLY, "Weekly"),
        (SCHEDULE_DAYS, "Specific days"),
        (SCHEDULE_INTERVAL, "Custom interval"),
    ]

    CATEGORY_HEALTH = "health"
    CATEGORY_STUDY = "study"
    CATEGORY_WORK = "work"
    CATEGORY_PERSONAL = "personal"
    CATEGORY_CHOICES = [
        (CATEGORY_HEALTH, "Health"),
        (CATEGORY_STUDY, "Study"),
        (CATEGORY_WORK, "Work"),
        (CATEGORY_PERSONAL, "Personal"),
    ]

    PRIORITY_HIGH = "high"
    PRIORITY_MEDIUM = "medium"
    PRIORITY_LOW = "low"
    PRIORITY_CHOICES = [
        (PRIORITY_HIGH, "High"),
        (PRIORITY_MEDIUM, "Medium"),
        (PRIORITY_LOW, "Low"),
    ]

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    name = models.CharField(max_length=120)
    description = models.TextField(blank=True)
    habit_type = models.CharField(
        max_length=14,
        choices=HABIT_TYPE_CHOICES,
        default=HABIT_BINARY,
    )
    target_value = models.DecimalField(
        max_digits=8,
        decimal_places=2,
        null=True,
        blank=True,
    )
    unit = models.CharField(max_length=24, blank=True)
    schedule_type = models.CharField(max_length=12, choices=SCHEDULE_CHOICES)
    category = models.CharField(
        max_length=12,
        choices=CATEGORY_CHOICES,
        default=CATEGORY_PERSONAL,
    )
    priority = models.CharField(
        max_length=6,
        choices=PRIORITY_CHOICES,
        default=PRIORITY_MEDIUM,
    )
    tags = models.CharField(max_length=255, blank=True)
    sort_order = models.PositiveIntegerField(default=0)
    start_date = models.DateField(default=timezone.localdate)
    interval_days = models.PositiveSmallIntegerField(default=1)
    weekly_interval = models.PositiveSmallIntegerField(default=1)
    days_of_week = models.CharField(max_length=20, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["sort_order", "name"]

    def __str__(self):
        return self.name

    def get_days_of_week_set(self):
        if not self.days_of_week:
            return set()
        return {int(value) for value in self.days_of_week.split(",") if value.strip()}

    def get_tags(self):
        if not self.tags:
            return []
        return [tag.strip() for tag in self.tags.split(",") if tag.strip()]

    def is_scheduled_on(self, target_date):
        if target_date < self.start_date:
            return False
        if self.schedule_type == self.SCHEDULE_DAILY:
            return True
        if self.schedule_type == self.SCHEDULE_WEEKLY:
            interval = max(1, self.weekly_interval) * 7
            return (target_date - self.start_date).days % interval == 0
        if self.schedule_type == self.SCHEDULE_INTERVAL:
            interval = max(1, self.interval_days)
            return (target_date - self.start_date).days % interval == 0
        if self.schedule_type == self.SCHEDULE_DAYS:
            return target_date.weekday() in self.get_days_of_week_set()
        return False

    @property
    def schedule_summary(self):
        if self.schedule_type == self.SCHEDULE_DAILY:
            return "Every day"
        if self.schedule_type == self.SCHEDULE_WEEKLY:
            day_label = self.start_date.strftime("%A")
            if self.weekly_interval == 1:
                return f"Every week on {day_label}"
            return f"Every {self.weekly_interval} weeks on {day_label}"
        if self.schedule_type == self.SCHEDULE_INTERVAL:
            if self.interval_days == 1:
                return "Every day"
            return f"Every {self.interval_days} days"
        if self.schedule_type == self.SCHEDULE_DAYS:
            days = self.get_days_of_week_set()
            if not days:
                return "Specific days"
            labels = [
                "Mon",
                "Tue",
                "Wed",
                "Thu",
                "Fri",
                "Sat",
                "Sun",
            ]
            ordered = [labels[idx] for idx in range(7) if idx in days]
            return "Every " + ", ".join(ordered)
        return ""


class HabitCompletion(models.Model):
    habit = models.ForeignKey(Habit, on_delete=models.CASCADE)
    date = models.DateField()
    completion_percentage = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal("0"),
        validators=[MinValueValidator(0), MaxValueValidator(100)],
    )
    raw_value = models.DecimalField(
        max_digits=8,
        decimal_places=2,
        null=True,
        blank=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("habit", "date")
        ordering = ["-date"]
        indexes = [
            models.Index(fields=["habit", "date"]),
        ]

    def __str__(self):
        return f"{self.habit.name} - {self.date}"

    @property
    def is_completed(self):
        return self.completion_percentage == Decimal("100")
