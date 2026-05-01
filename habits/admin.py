from django.contrib import admin

from .models import Habit, HabitCompletion


@admin.register(Habit)
class HabitAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "user",
        "habit_type",
        "category",
        "priority",
        "schedule_type",
        "start_date",
        "target_value",
        "unit",
        "sort_order",
    )
    list_filter = ("habit_type", "category", "priority", "schedule_type", "start_date")
    search_fields = ("name", "description", "tags", "user__username")


@admin.register(HabitCompletion)
class HabitCompletionAdmin(admin.ModelAdmin):
    list_display = ("habit", "date", "completion_percentage", "raw_value")
    list_filter = ("date",)
    search_fields = ("habit__name", "habit__user__username")
