from django.contrib import admin

from .models import Habit, HabitCompletion


@admin.register(Habit)
class HabitAdmin(admin.ModelAdmin):
    list_display = ("name", "user", "schedule_type", "start_date")
    list_filter = ("schedule_type", "start_date")
    search_fields = ("name", "description", "user__username")


@admin.register(HabitCompletion)
class HabitCompletionAdmin(admin.ModelAdmin):
    list_display = ("habit", "date", "completed")
    list_filter = ("completed", "date")
    search_fields = ("habit__name", "habit__user__username")
