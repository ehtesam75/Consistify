from django.contrib import admin

from .models import (
    FriendRequest,
    Habit,
    HabitCompletion,
    HabitPause,
    HabitPlanVersion,
    ProgressSharing,
)
from .plan_versions import ensure_initial_plan_version


@admin.register(Habit)
class HabitAdmin(admin.ModelAdmin):
    versioned_fields = (
        "schedule_type",
        "categories",
        "priority",
        "start_date",
        "interval_days",
        "weekly_interval",
        "days_of_week",
    )
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

    def get_readonly_fields(self, request, obj=None):
        readonly_fields = tuple(super().get_readonly_fields(request, obj))
        if obj is not None:
            return readonly_fields + self.versioned_fields
        return readonly_fields

    def save_related(self, request, form, formsets, change):
        super().save_related(request, form, formsets, change)
        ensure_initial_plan_version(form.instance)

    def category_list(self, obj):
        return ", ".join(category.label for category in obj.categories.all())

    category_list.short_description = "Categories"


@admin.register(HabitPlanVersion)
class HabitPlanVersionAdmin(admin.ModelAdmin):
    list_display = (
        "habit",
        "effective_from",
        "schedule_type",
        "schedule_anchor",
        "priority",
        "category_list",
    )
    list_filter = ("effective_from", "schedule_type", "priority", "categories")
    search_fields = ("habit__name", "habit__user__username")
    fields = (
        "habit",
        "effective_from",
        "schedule_type",
        "schedule_anchor",
        "interval_days",
        "weekly_interval",
        "days_of_week",
        "priority",
        "category_list",
        "created_at",
        "updated_at",
    )
    readonly_fields = fields

    def category_list(self, obj):
        return ", ".join(category.label for category in obj.categories.all())

    category_list.short_description = "Categories"

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(HabitCompletion)
class HabitCompletionAdmin(admin.ModelAdmin):
    list_display = ("habit", "date", "completion_percentage", "raw_value")
    list_filter = ("date",)
    search_fields = ("habit__name", "habit__user__username")


@admin.register(FriendRequest)
class FriendRequestAdmin(admin.ModelAdmin):
    list_display = ("from_user", "to_user", "status", "created_at", "updated_at")
    list_filter = ("status", "created_at")
    search_fields = ("from_user__username", "to_user__username")
    readonly_fields = ("friendship_key", "created_at", "updated_at")


@admin.register(ProgressSharing)
class ProgressSharingAdmin(admin.ModelAdmin):
    list_display = (
        "user_one",
        "user_two",
        "requester",
        "status",
        "requested_at",
        "accepted_at",
    )
    list_filter = ("status", "requested_at", "accepted_at")
    search_fields = (
        "user_one__username",
        "user_two__username",
        "requester__username",
    )
    readonly_fields = (
        "friendship",
        "user_one",
        "user_two",
        "requester",
        "status",
        "requested_at",
        "accepted_at",
        "updated_at",
    )

    def has_add_permission(self, request):
        return False


@admin.register(HabitPause)
class HabitPauseAdmin(admin.ModelAdmin):
    list_display = ("habit", "start_date", "end_date", "created_at", "updated_at")
    list_filter = ("start_date", "end_date")
    search_fields = ("habit__name", "habit__user__username")
