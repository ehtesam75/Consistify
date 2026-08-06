from decimal import Decimal

from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils import timezone


DEFAULT_CATEGORIES = [
    ("health", "Health"),
    ("study", "Study"),
    ("organize", "Organize"),
    ("good-deeds", "Good deeds"),
    ("self-development", "Self development"),
    ("spiritual", "Spiritual"),
]


class HabitCategory(models.Model):
    key = models.SlugField(max_length=32, unique=True)
    label = models.CharField(max_length=40)
    sort_order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["sort_order", "label"]

    def __str__(self):
        return self.label


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
    categories = models.ManyToManyField(
        HabitCategory,
        blank=True,
        related_name="habits",
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

    def active_pause(self):
        cache = getattr(self, "_prefetched_objects_cache", None)
        if cache and "pauses" in cache:
            pauses = [pause for pause in cache["pauses"] if pause.end_date is None]
            if not pauses:
                return None
            return sorted(pauses, key=lambda pause: pause.start_date, reverse=True)[0]
        return self.pauses.filter(end_date__isnull=True).order_by("-start_date").first()

    def is_paused_on(self, target_date):
        cache = getattr(self, "_prefetched_objects_cache", None)
        if cache and "pauses" in cache:
            for pause in cache["pauses"]:
                if pause.start_date <= target_date and (
                    pause.end_date is None or target_date < pause.end_date
                ):
                    return True
            return False
        return self.pauses.filter(start_date__lte=target_date).filter(
            models.Q(end_date__isnull=True) | models.Q(end_date__gt=target_date)
        ).exists()

    @property
    def is_paused(self):
        return self.is_paused_on(timezone.localdate())

    def is_scheduled_on(self, target_date):
        from .services import is_habit_scheduled_on

        return is_habit_scheduled_on(self, target_date)

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


class HabitPlanVersion(models.Model):
    """Effective-dated scoring and scheduling configuration for a habit."""

    habit = models.ForeignKey(
        Habit,
        on_delete=models.CASCADE,
        related_name="plan_versions",
    )
    effective_from = models.DateField()
    schedule_anchor = models.DateField()
    schedule_type = models.CharField(max_length=12, choices=Habit.SCHEDULE_CHOICES)
    interval_days = models.PositiveSmallIntegerField(default=1)
    weekly_interval = models.PositiveSmallIntegerField(default=1)
    days_of_week = models.CharField(max_length=20, blank=True)
    priority = models.CharField(
        max_length=6,
        choices=Habit.PRIORITY_CHOICES,
        default=Habit.PRIORITY_MEDIUM,
    )
    categories = models.ManyToManyField(
        HabitCategory,
        blank=True,
        related_name="habit_plan_versions",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["effective_from", "id"]
        indexes = [
            models.Index(
                fields=["habit", "effective_from"],
                name="habit_plan_effective_idx",
            ),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["habit", "effective_from"],
                name="habit_plan_effective_unique",
            ),
            models.CheckConstraint(
                condition=models.Q(interval_days__gte=1),
                name="habit_plan_interval_gte_1",
            ),
            models.CheckConstraint(
                condition=models.Q(weekly_interval__gte=1),
                name="habit_plan_weekly_gte_1",
            ),
        ]

    def __str__(self):
        return f"{self.habit.name} plan from {self.effective_from}"

    def get_days_of_week_set(self):
        if not self.days_of_week:
            return set()
        return {
            int(value)
            for value in self.days_of_week.split(",")
            if value.strip()
        }


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


class HabitPause(models.Model):
    habit = models.ForeignKey(
        Habit,
        on_delete=models.CASCADE,
        related_name="pauses",
    )
    start_date = models.DateField()
    end_date = models.DateField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-start_date"]
        indexes = [
            models.Index(fields=["habit", "start_date"], name="habits_pause_habit_start_idx"),
            models.Index(fields=["habit", "end_date"], name="habits_pause_habit_end_idx"),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["habit"],
                condition=models.Q(end_date__isnull=True),
                name="habits_pause_active_unique",
            ),
        ]

    def __str__(self):
        state = "active" if self.end_date is None else "ended"
        return f"{self.habit.name} pause ({state})"


class FriendRequest(models.Model):
    STATUS_PENDING = "pending"
    STATUS_ACCEPTED = "accepted"
    STATUS_CHOICES = [
        (STATUS_PENDING, "Pending"),
        (STATUS_ACCEPTED, "Accepted"),
    ]

    from_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="sent_friend_requests",
    )
    to_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="received_friend_requests",
    )
    friendship_key = models.CharField(max_length=64, unique=True, editable=False)
    status = models.CharField(
        max_length=12,
        choices=STATUS_CHOICES,
        default=STATUS_PENDING,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["to_user", "status"]),
            models.Index(fields=["from_user", "status"]),
        ]
        constraints = [
            models.CheckConstraint(
                condition=~models.Q(from_user=models.F("to_user")),
                name="friend_request_not_self",
            ),
        ]

    def __str__(self):
        return f"{self.from_user} -> {self.to_user} ({self.status})"

    @classmethod
    def build_friendship_key(cls, first_user_id, second_user_id):
        first, second = sorted([int(first_user_id), int(second_user_id)])
        return f"{first}:{second}"

    def save(self, *args, **kwargs):
        if self.from_user_id and self.to_user_id:
            if self.from_user_id == self.to_user_id:
                raise ValueError("Users cannot send friend requests to themselves.")
            self.friendship_key = self.build_friendship_key(
                self.from_user_id,
                self.to_user_id,
            )
        super().save(*args, **kwargs)

    def accept(self):
        self.status = self.STATUS_ACCEPTED
        self.updated_at = timezone.now()
        self.save(update_fields=["status", "updated_at"])
