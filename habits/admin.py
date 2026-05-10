from django.contrib import admin

from .models import Habit, HabitCompletion


@admin.register(Habit)
class HabitAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "user",
        "habit_type",
        "category_list",
        "priority",
        "schedule_type",
        "start_date",
        "target_value",
        "unit",
        "sort_order",
    )
    list_filter = ("habit_type", "categories", "priority", "schedule_type", "start_date")
    search_fields = ("name", "description", "tags", "user__username")
    filter_horizontal = ("categories",)

    def category_list(self, obj):
        return ", ".join(category.label for category in obj.categories.all())

    category_list.short_description = "Categories"


@admin.register(HabitCompletion)
class HabitCompletionAdmin(admin.ModelAdmin):
    list_display = ("habit", "date", "completion_percentage", "raw_value")
    list_filter = ("date",)
    search_fields = ("habit__name", "habit__user__username")
