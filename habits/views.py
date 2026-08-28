import json
import logging
import secrets
from datetime import timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from math import ceil

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model, login
from django.contrib.auth import views as auth_views
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import UserCreationForm
from django.db import DEFAULT_DB_ALIAS, IntegrityError, transaction
from django.db.models import Max, Q
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .forms import HabitForm
from .models import (
    FriendRequest,
    Habit,
    HabitCategory,
    HabitCompletion,
    HabitPause,
    ProgressSharing,
)
from .plan_versions import ensure_initial_plan_version, schedule_habit_plan_edit
from .services import (
    ANALYTICS_CUTOFF_NOTE,
    analytics_end_date,
    analytics_window,
    build_category_analytics,

    build_habit_score_drivers,
    build_monthly_reports,
    build_overall_score_breakdown,
    build_weekly_reports,
    can_update_progress_on,
    calculate_streaks,
    completion_stats,
    compute_today_metrics,
    compute_user_metrics,
    daily_recap_target_date,
    daily_average_completion_series,
    get_pending_habits_for_date,
    get_shared_yesterday_progress,
    get_completion_maps,
    get_next_scheduled_date,
    habit_tracking_start,
    habit_performance_metrics,
    iter_scheduled_occurrences,
    leaderboard_ranking_score,
    LEADERBOARD_CONFIDENCE_SESSIONS,
    mark_daily_recap_completed,
    quantitative_value_period_start,
    resolve_habit_plan_on,

    should_show_daily_recap,
    iter_scheduled_dates,
    weighted_completion_rate_from_totals,
)


logger = logging.getLogger(__name__)


def _attach_active_plan_display(habits, target_date):
    """Attach date-specific plan fields used by habit summary templates."""
    resolved = []
    category_ids = set()
    for habit in habits:
        config = resolve_habit_plan_on(habit, target_date)
        resolved.append((habit, config))
        if config is not None:
            category_ids.update(config.category_ids)

    categories = HabitCategory.objects.in_bulk(category_ids)
    for habit, config in resolved:
        if config is None:
            habit.active_schedule_summary = habit.schedule_summary
            habit.active_priority = habit.priority
            habit.active_priority_label = habit.get_priority_display()
            habit.active_categories = list(habit.categories.all())
        else:
            habit.active_schedule_summary = config.schedule_summary
            habit.active_priority = config.priority
            habit.active_priority_label = config.priority_label
            habit.active_categories = sorted(
                (
                    categories[category_id]
                    for category_id in config.category_ids
                    if category_id in categories
                ),
                key=lambda category: (category.sort_order, category.label),
            )

        prefetched = getattr(habit, "_prefetched_objects_cache", None) or {}
        versions = prefetched.get("plan_versions")
        if versions is None:
            versions = habit.plan_versions.filter(
                effective_from__gt=target_date
            ).only("effective_from")
        future_dates = [
            version.effective_from
            for version in versions
            if version.effective_from > target_date
        ]
        habit.pending_plan_date = min(future_dates) if future_dates else None


def health(request):
    return HttpResponse("OK", content_type="text/plain")


def index(request):
    if request.user.is_authenticated:
        return redirect("habits:today")
    return render(request, "habits/home.html")


class ConsistifyLoginView(auth_views.LoginView):
    template_name = "registration/login.html"

    def form_valid(self, form):
        user = form.get_user()
        response = super().form_valid(form)
        messages.success(self.request, "Logged in successfully.")

        # ``login()`` rotates the session key, so the fresh session starts with
        # no recap state at all. Re-derive the prompt purely from persisted
        # database state so a recap already finished on another device, browser,
        # or session stays hidden after this login, while one that is still
        # unfinished is seeded here and can be submitted immediately.
        today = timezone.localdate()
        should_show, recap_date, _ = should_show_daily_recap(user, today)

        if should_show:
            self.request.session["daily_recap_date"] = recap_date.isoformat()
        else:
            self.request.session.pop("daily_recap_date", None)
        return response


class ConsistifyLogoutView(auth_views.LogoutView):
    next_page = "habits:index"

    def post(self, request, *args, **kwargs):
        response = super().post(request, *args, **kwargs)
        messages.success(request, "Logged out successfully.")
        return response


def _build_today_habit_context(request):
    today = timezone.localdate()
    target_date = _get_date_from_request(request)
    all_habits = list(
        Habit.objects.filter(user=request.user)
        .prefetch_related("categories", "pauses", "plan_versions__categories")
        .order_by("sort_order", "name")
    )
    _attach_active_plan_display(all_habits, today)
    habits = [habit for habit in all_habits if not habit.is_archived]
    archived_habits = [habit for habit in all_habits if habit.is_archived]
    active_habits = [habit for habit in habits if not habit.is_paused_on(target_date)]


    today_metrics = compute_today_metrics(request.user, target_date)
    scheduled_habits = today_metrics["rows"]

    session_hide_completed = request.session.get("today_hide_completed")
    if request.GET.get("hide_completed") is not None:
        hide_completed = request.GET.get("hide_completed") == "1"
        request.session["today_hide_completed"] = hide_completed
    else:
        hide_completed = (
            session_hide_completed if isinstance(session_hide_completed, bool) else False
        )

    visible_scheduled_habits = (
        [item for item in scheduled_habits if not item["completed"]]
        if hide_completed
        else scheduled_habits
    )

    tomorrow = today + timedelta(days=1)
    if habits:
        all_paused = True
        all_pause_scheduled = True
        for habit in habits:
            active_pause = habit.active_pause()
            if not habit.is_paused_on(today):
                all_paused = False
            if not (active_pause and active_pause.start_date == tomorrow):
                all_pause_scheduled = False
    else:
        all_paused = False
        all_pause_scheduled = False

    return {
        "target_date": target_date,
        "today": today,
        "prev_date": target_date - timedelta(days=1),
        "next_date": target_date + timedelta(days=1),
        "can_edit_progress": can_update_progress_on(target_date, today),
        "scheduled_habits": visible_scheduled_habits,
        "scheduled_count": today_metrics["scheduled_count"],
        "completed_count": today_metrics["completed_count"],
        "completion_rate": today_metrics["completion_rate"],
        "hide_completed": hide_completed,
        "visible_scheduled_count": len(visible_scheduled_habits),
        "habits": habits,
        "archived_habits": archived_habits,
        "archived_count": len(archived_habits),
        "all_count": len(active_habits),
        "all_paused": all_paused,
        "all_pause_scheduled": all_pause_scheduled,
    }



@login_required
def habit_list(request):
    context = _build_today_habit_context(request)
    return render(request, "habits/habit_list.html", context)


@login_required
def mobile_all_habits(request):
    context = _build_today_habit_context(request)
    return render(request, "habits/mobile_all_habits.html", context)


@login_required
def archived_habits(request):
    today = timezone.localdate()
    habits = list(
        Habit.objects.filter(user=request.user, archived_at__isnull=False)
        .prefetch_related("categories", "pauses", "plan_versions__categories")
        .order_by("sort_order", "name")
    )
    _attach_active_plan_display(habits, today)
    context = {
        "today": today,
        "archived_habits": habits,
        "archived_count": len(habits),
    }
    return render(request, "habits/archived_habits.html", context)



@login_required
def dashboard(request):
    today = timezone.localdate()
    # Every number on this page is analytics, so the whole window ends at the
    # finalized cutoff. ``analytics_window`` keeps the full 30-day length by
    # moving the start back with the end, rather than shortening the period to
    # 29 days by trimming only the end.
    window_start, window_end = analytics_window(30, today)
    dashboard_window_note = "Last 30 days"
    previous_window_start, previous_window_end = _previous_period_for_window(
        window_start,
        window_end,
    )

    habits = list(
        Habit.objects.filter(user=request.user)
        .prefetch_related("categories", "pauses", "plan_versions__categories")
        .order_by("sort_order", "name")
    )
    total_habits = sum(
        1 for habit in habits if not habit.is_paused and not habit.is_archived
    )

    aggregated = compute_user_metrics(habits, window_start, window_end)

    habit_cards = aggregated["per_habit"]

    aggregate = aggregated["aggregate"]
    total_scheduled = aggregate["total_scheduled"]
    total_completed = aggregate["total_completed"]
    overall_rate = aggregate["completion_rate"]
    # Reuse the per-habit metrics already computed by ``compute_user_metrics``
    # so the breakdown and drivers do not re-iterate the habits and re-query
    # the completion maps for the same window.
    score_breakdown = build_overall_score_breakdown(
        habits,
        window_start,
        window_end,
        previous_window_start,
        previous_window_end,
        precomputed_metrics=habit_cards,
    )
    score_breakdown["current_period_label"] = _format_period_label(
        window_start,
        window_end,
    )

    score_breakdown["previous_period_label"] = (
        _format_period_label(previous_window_start, previous_window_end)
        if previous_window_start and previous_window_end
        else ""
    )
    overall_consistency = score_breakdown["current_score"]
    score_drivers = build_habit_score_drivers(
        habits,
        window_start,
        window_end,
        previous_window_start,
        previous_window_end,
        precomputed_metrics=habit_cards,
    )
    category_analytics = build_category_analytics(habits, window_start, window_end)


    doing_well = [card for card in habit_cards if card["scheduled"] and card["rate"] >= 80]
    needs_focus = [card for card in habit_cards if card["scheduled"] and card["rate"] < 50]

    # The trend chart is analytics too, so it plots 14 finalized days ending
    # yesterday. Labels come from the same series, so the x-axis never shows
    # today's date for a point that was deliberately excluded.
    chart_start, chart_end = analytics_window(14, today)
    chart_series = daily_average_completion_series(habits, chart_start, chart_end)
    chart_labels = [item["label"] for item in chart_series]
    chart_rates = [item["value"] for item in chart_series]


    context = {
        "today": today,
        "habits": habits,
        "habit_cards": habit_cards,
        "overall_rate": overall_rate,
        "overall_consistency": overall_consistency,
        "total_scheduled": total_scheduled,
        "total_completed": total_completed,
        "total_habits": total_habits,
        "doing_well": doing_well,
        "needs_focus": needs_focus,
        "score_breakdown": score_breakdown,
        "score_driver_cards": _build_score_driver_cards(score_drivers),
        "category_analytics": category_analytics,
        "chart_labels": json.dumps(chart_labels),
        "chart_rates": json.dumps(chart_rates),
        "dashboard_window_label": _format_period_label(window_start, window_end),
        "dashboard_window_note": dashboard_window_note,
        "analytics_cutoff_note": ANALYTICS_CUTOFF_NOTE,
        "analytics_end_date": window_end,
    }
    return render(request, "habits/dashboard.html", context)



@login_required
def habit_detail(request, habit_id):
    habit = get_object_or_404(
        Habit.objects.prefetch_related(
            "categories",
            "pauses",
            "plan_versions__categories",
        ),
        id=habit_id,
        user=request.user,
    )
    today = timezone.localdate()
    _attach_active_plan_display([habit], today)
    all_time_start = habit_tracking_start(habit)
    history_dates = list(iter_scheduled_dates(habit, all_time_start, today))
    history_limit = 15
    recent_history_dates = history_dates[-history_limit:]
    # The chart is analytics: it plots finalized sessions only, so no x-axis
    # label advertises today's date for data that is deliberately excluded.
    # The history list keeps today's row as a live "Pending/Done" status.
    finalized_history_dates = [
        scheduled_date
        for scheduled_date in history_dates
        if scheduled_date <= analytics_end_date(today)
    ]
    chart_dates = finalized_history_dates[-history_limit:]


    completion_map, value_map = get_completion_maps(habit, all_time_start, today)
    history = []
    for scheduled_date in recent_history_dates:
        completion_percentage = completion_map.get(scheduled_date, 0)
        completed = completion_percentage >= 100
        if completed:
            status = "done"
            status_label = "Done"
        elif scheduled_date == today:
            status = "pending"
            status_label = "Pending"
        else:
            status = "missed"
            status_label = "Missed"
        history.append(
            {
                "date": scheduled_date,
                "completion_percentage": completion_percentage,
                "completed": completed,
                "status": status,
                "status_label": status_label,
            }
        )

    window_start, window_end = analytics_window(15, today)
    # If the habit hasn't been tracked for the full 15 days, start from its
    # immutable tracking start instead of pretending a full window exists.
    effective_start = max(window_start, all_time_start)
    window_completion_map = {
        date: value
        for date, value in completion_map.items()
        if effective_start <= date <= window_end
    }
    window_value_map = {
        date: value
        for date, value in value_map.items()
        if effective_start <= date <= window_end
    }
    stats = completion_stats(
        habit,
        effective_start,
        window_end,
        window_completion_map,
        window_value_map,
    )
    all_time_stats = completion_stats(
        habit,
        all_time_start,
        today,
        completion_map,
        value_map,
    )
    # The Average Daily Value only averages readings that share the same
    # meaning. A change to the target, the unit, or the habit type (into a
    # quantitative configuration) restarts the averaging period, while
    # schedule / priority / category / name changes do not. When no relevant
    # change has ever occurred this resolves to the habit's tracking start, so
    # the full history is used exactly as before. The end is still clamped to
    # yesterday inside ``completion_stats`` (finalized-analytics rule).
    value_period_start = quantitative_value_period_start(habit, today)
    avg_value_stats = completion_stats(
        habit,
        value_period_start,
        today,
        completion_map,
        value_map,
    )
    detailed_metrics = habit_performance_metrics(

        habit,
        all_time_start,
        today,
        completion_map,
        value_map,
    )
    current_streak, max_streak = calculate_streaks(habit, today)

    today_completion = HabitCompletion.objects.filter(habit=habit, date=today).first()
    today_completion_percentage = (
        float(today_completion.completion_percentage) if today_completion else 0.0
    )
    today_raw_value = None
    if today_completion and today_completion.raw_value is not None:
        today_raw_value = float(today_completion.raw_value)

    today_completed = today_completion_percentage >= 100
    is_scheduled_today = habit.is_scheduled_on(today)
    next_due = get_next_scheduled_date(habit, today)

    chart_labels = json.dumps([date.strftime("%b %d") for date in chart_dates])
    chart_values = []
    for scheduled_date in chart_dates:
        chart_values.append(completion_map.get(scheduled_date, 0))
    chart_percentages = json.dumps(chart_values)

    context = {
        "habit": habit,
        "history": history,
        "stats": stats,
        "all_time_stats": all_time_stats,
        "avg_value_stats": avg_value_stats,
        "metrics": detailed_metrics,

        "current_streak": current_streak,
        "max_streak": max_streak,
        "today": today,
        "today_completed": today_completed,
        "today_completion_percentage": today_completion_percentage,
        "today_raw_value": today_raw_value,
        "is_scheduled_today": is_scheduled_today,
        "next_due": next_due,
        "chart_labels": chart_labels,
        "chart_percentages": chart_percentages,
        "tags": habit.get_tags(),
        "detail_window_note": "Last 15 days",
        "completion_window_label": f"Since {effective_start.strftime('%b %d, %Y')}",
        "all_time_label": f"Since {all_time_start.strftime('%b %d, %Y')}",
        # The Average Daily Value "Since" date must exactly match the date the
        # average is calculated from, which restarts on the latest relevant
        # quantitative plan change (target / unit / habit-type).
        "avg_value_label": f"Since {value_period_start.strftime('%b %d, %Y')}",
        "analytics_cutoff_note": ANALYTICS_CUTOFF_NOTE,


    }
    return render(request, "habits/habit_detail.html", context)


@login_required
def habit_create(request):
    if request.method == "POST":
        form = HabitForm(request.POST)
        if form.is_valid():
            habit = form.save(commit=False)
            habit.user = request.user
            max_order = (
                Habit.objects.filter(user=request.user).aggregate(Max("sort_order"))[
                    "sort_order__max"
                ]
                or 0
            )
            habit.sort_order = max_order + 1
            habit.save()
            form.save_m2m()
            ensure_initial_plan_version(habit)
            messages.success(request, "Habit created.")
            return redirect("habits:habit_detail", habit_id=habit.id)
    else:
        form = HabitForm()

    return render(
        request,
        "habits/habit_form.html",
        {
            "form": form,
            "title": "Create habit",
            "submit_label": "Create habit",
        },
    )


@login_required
def habit_edit(request, habit_id):
    habit = get_object_or_404(
        Habit.objects.prefetch_related("pauses"),
        id=habit_id,
        user=request.user,
    )
    today = timezone.localdate()
    if request.method == "POST":
        form = HabitForm(request.POST, instance=habit)
        if form.is_valid():
            versioned_fields = {
                "habit_type",
                "target_value",
                "unit",
                "schedule_type",
                "categories",
                "priority",
                "start_date",
                "interval_days",
                "weekly_interval",
                "days_of_week",
            }
            plan_changed = bool(versioned_fields.intersection(form.changed_data))
            if plan_changed:
                non_plan_values = {
                    field: getattr(form.instance, field)
                    for field in (
                        "name",
                        "description",
                        "tags",
                    )
                }
                database = habit._state.db or DEFAULT_DB_ALIAS
                with transaction.atomic(using=database):
                    schedule_habit_plan_edit(
                        habit,
                        habit_type=form.cleaned_data["habit_type"],
                        target_value=form.cleaned_data["target_value"],
                        unit=form.cleaned_data["unit"],
                        schedule_type=form.cleaned_data["schedule_type"],
                        start_date=form.cleaned_data["start_date"],
                        interval_days=form.cleaned_data["interval_days"] or 1,
                        weekly_interval=form.cleaned_data["weekly_interval"] or 1,
                        days_of_week=form.cleaned_data["days_of_week"],
                        priority=form.cleaned_data["priority"],
                        categories=form.cleaned_data["categories"],
                        today=today,
                    )
                    for field, value in non_plan_values.items():
                        setattr(habit, field, value)
                    habit.save(update_fields=[*non_plan_values, "updated_at"])
                messages.success(
                    request,
                    "Habit updated. Plan changes take effect tomorrow.",
                )
            else:
                form.save()
                ensure_initial_plan_version(habit)
                messages.success(request, "Habit updated.")
            return redirect("habits:habit_detail", habit_id=habit.id)
    else:
        form = HabitForm(instance=habit)

    return render(
        request,
        "habits/habit_form.html",
        {
            "form": form,
            "title": "Edit habit",
            "submit_label": "Save changes",
            "today": today,
        },
    )


@login_required
@require_POST
def habit_delete(request, habit_id):
    habit = get_object_or_404(Habit, id=habit_id, user=request.user)
    habit_name = habit.name
    habit.delete()
    messages.success(request, f'"{habit_name}" deleted.')

    next_url = request.POST.get("next")
    return redirect(next_url or reverse("habits:today"))


@login_required
@require_POST
def archive_habit(request, habit_id):
    habit = get_object_or_404(Habit, id=habit_id, user=request.user)
    if habit.is_archived:
        messages.info(request, f'"{habit.name}" is already archived.')
    else:
        effective_date = timezone.localdate() + timedelta(days=1)
        habit.archive(effective_date=effective_date)
        messages.success(
            request,
            f'"{habit.name}" archived starting tomorrow. '
            "All history and reports are preserved.",
        )

    return redirect(
        _safe_next_url(request) or reverse("habits:habit_detail", args=[habit.id])
    )


@login_required
@require_POST
def unarchive_habit(request, habit_id):
    habit = get_object_or_404(Habit, id=habit_id, user=request.user)
    if not habit.is_archived:
        messages.info(request, "This habit is not archived.")
    else:
        habit.unarchive()
        messages.success(request, f'"{habit.name}" restored to active tracking.')

    return redirect(
        _safe_next_url(request) or reverse("habits:habit_detail", args=[habit.id])
    )



@login_required
@require_POST
def pause_habit(request, habit_id):
    habit = get_object_or_404(Habit, id=habit_id, user=request.user)
    start_date = timezone.localdate() + timedelta(days=1)
    if _schedule_habit_pause(habit, start_date):
        messages.success(request, f'"{habit.name}" paused starting tomorrow.')
    else:
        messages.info(request, "This habit is already paused or scheduled to pause.")

    return redirect(_safe_next_url(request) or reverse("habits:habit_detail", args=[habit.id]))


@login_required
@require_POST
def pause_all_habits(request):
    habits = list(
        Habit.objects.filter(user=request.user, archived_at__isnull=True)
        .prefetch_related("pauses")
        .order_by("sort_order", "name")
    )
    start_date = timezone.localdate() + timedelta(days=1)

    paused_count = sum(
        1 for habit in habits if _schedule_habit_pause(habit, start_date)
    )

    if paused_count:
        habit_label = "habit" if paused_count == 1 else "habits"
        messages.success(
            request,
            f"{paused_count} {habit_label} paused starting tomorrow.",
        )
    elif habits:
        messages.info(request, "All habits are already paused or scheduled to pause.")
    else:
        messages.info(request, "No habits to pause yet.")

    return redirect(_safe_next_url(request) or reverse("habits:today"))


@login_required
@require_POST
def resume_all_habits(request):
    habits = list(
        Habit.objects.filter(user=request.user, archived_at__isnull=True)
        .prefetch_related("pauses")
        .order_by("sort_order", "name")
    )
    today = timezone.localdate()


    if not habits:
        messages.info(request, "No habits to resume yet.")
        return redirect(_safe_next_url(request) or reverse("habits:today"))

    tomorrow = today + timedelta(days=1)
    all_paused = True
    all_pause_scheduled = True
    for habit in habits:
        active_pause = habit.active_pause()
        if not habit.is_paused_on(today):
            all_paused = False
        if not (active_pause and active_pause.start_date == tomorrow):
            all_pause_scheduled = False

    updated_count = HabitPause.objects.filter(
        habit__in=habits,
        end_date__isnull=True,
    ).update(end_date=today, updated_at=timezone.now())

    if all_paused:
        messages.success(request, "All habits resumed.")
    elif all_pause_scheduled:
        messages.success(request, "All scheduled pauses canceled.")
    elif updated_count:
        messages.success(request, "Habit pauses cleared.")
    else:
        messages.info(request, "No habit pauses to update.")

    return redirect(_safe_next_url(request) or reverse("habits:today"))


@login_required
@require_POST
def resume_habit(request, habit_id):
    habit = get_object_or_404(Habit, id=habit_id, user=request.user)
    active_pause = habit.active_pause()
    if not active_pause:
        messages.info(request, "This habit is not paused.")
    else:
        active_pause.end_date = timezone.localdate()
        active_pause.save(update_fields=["end_date", "updated_at"])
        messages.success(request, f'"{habit.name}" resumed.')

    return redirect(_safe_next_url(request) or reverse("habits:habit_detail", args=[habit.id]))


def _schedule_habit_pause(habit, start_date):
    if habit.active_pause():
        return False

    try:
        with transaction.atomic():
            HabitPause.objects.create(habit=habit, start_date=start_date)
    except IntegrityError:
        return False
    return True


@login_required
@require_POST
def update_progress(request, habit_id):
    habit = get_object_or_404(Habit, id=habit_id, user=request.user)
    target_date = _parse_date_value(request.POST.get("date"))
    today = timezone.localdate()
    is_ajax = _wants_json(request)

    try:
        if target_date is None:
            return _progress_error_response(
                request,
                "Choose a valid date before updating progress.",
            )

        if not can_update_progress_on(target_date, today):
            return _progress_error_response(
                request,
                "You can only update progress for today and the previous day.",
            )

        if habit.is_paused_on(target_date):
            return _progress_error_response(request, "This habit is paused for that day.")

        if not habit.is_scheduled_on(target_date):
            return _progress_error_response(
                request,
                "This habit is not scheduled for that day.",
            )

        completion, _ = HabitCompletion.objects.get_or_create(
            habit=habit,
            date=target_date,
        )

        plan_config = resolve_habit_plan_on(habit, target_date)
        effective_habit_type = plan_config.habit_type if plan_config else habit.habit_type
        effective_target_value = (
            plan_config.target_value if plan_config else habit.target_value
        )

        completion_percentage = Decimal("0")
        raw_value = None

        if effective_habit_type == Habit.HABIT_BINARY:
            is_done = request.POST.get("completed") is not None
            completion_percentage = Decimal("100") if is_done else Decimal("0")
            raw_value = completion_percentage
        elif effective_habit_type == Habit.HABIT_PARTIAL:
            completion_percentage = _clamp_percentage(request.POST.get("completion_percentage"))
            raw_value = completion_percentage
        else:
            if not effective_target_value or effective_target_value <= 0:
                return _progress_error_response(
                    request,
                    "Add a target value before logging progress.",
                )
            target_value = effective_target_value.quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
            raw_value = _parse_decimal(request.POST.get("current_value")) or Decimal("0")
            raw_value = raw_value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

            if raw_value < 0:
                raw_value = Decimal("0")
            if raw_value > target_value:
                raw_value = target_value
            completion_percentage = _percentage_from_value(raw_value, target_value)

        completion.completion_percentage = completion_percentage
        completion.raw_value = raw_value
        completion.save(update_fields=["completion_percentage", "raw_value"])

        if is_ajax:
            today_metrics = compute_today_metrics(request.user, target_date)
            is_completed = completion_percentage >= Decimal("100")
            # Surface the "completed" flag and current "hide completed" setting
            # so the client can show/hide the row immediately on success,
            # without waiting for a page refresh. Sessions are already
            # populated for AJAX callers (the page rendered the form), so
            # falling back to False when absent is safe.
            session_hide_completed = request.session.get("today_hide_completed")
            hide_completed = (
                session_hide_completed
                if isinstance(session_hide_completed, bool)
                else False
            )
            return JsonResponse({
                "ok": True,
                "completion_percentage": float(completion_percentage),
                "raw_value": float(raw_value) if raw_value is not None else None,
                "completed_count": today_metrics["completed_count"],
                "scheduled_count": today_metrics["scheduled_count"],
                "completion_rate": today_metrics["completion_rate"],
                "completed": is_completed,
                "hide_completed": hide_completed,
            })

        return redirect(_safe_next_url(request) or reverse("habits:today"))
    except Exception:
        # Database/network errors or unexpected conditions still surface as
        # safe JSON for AJAX callers and friendly messages for normal browsers.
        logger.exception(
            "update_progress failed for habit_id=%s by user_id=%s",
            habit_id,
            getattr(request.user, "id", None),
        )
        return _progress_error_response(
            request,
            "Could not save your progress right now. Please try again.",
        )


@csrf_exempt
def cron_job(request):
    """External cron entry point.

    The view is intentionally tolerant: any unexpected exception is caught
    here and re-emitted as a JSON 500. Combined with the structured exception
    middleware, the cron provider receives a predictable response shape and
    the failure is fully logged on the server side.
    """
    if request.method != "POST":
        return JsonResponse({"ok": False, "error": "Method not allowed."}, status=405)

    authorization_header = request.headers.get("Authorization", "")
    provided_secret = _extract_authorization_token(authorization_header)

    if not settings.CRON_SECRET or not provided_secret or not secrets.compare_digest(
        provided_secret,
        settings.CRON_SECRET,
    ):
        return JsonResponse({"ok": False, "error": "Unauthorized."}, status=401)

    try:
        _run_cron_job()
    except Exception:
        # Re-raise so the structured exception middleware records the full
        # traceback with the request payload, then return a safe JSON
        # envelope so the cron provider sees a clean failure response.
        logger.exception("Cron job failed during execution.")
        return JsonResponse(
            {"ok": False, "error": "Cron job failed.", "code": "cron_failed"},
            status=500,
        )
    logger.info("Cron job executed successfully.")
    return JsonResponse({"ok": True, "message": "Cron job executed successfully."})


@login_required
@require_POST
def reorder_habits(request):
    raw_ids = request.POST.get("habit_ids", "")
    try:
        ordered_ids = [int(value) for value in raw_ids.split(",") if value.strip()]
    except ValueError:
        return JsonResponse({"ok": False, "error": "Invalid habit ids."}, status=400)

    try:
        # Archived habits are shown in a separate, non-draggable list, so
        # the reorder payload only needs to be a valid subset of the
        # user's habits.
        user_habit_ids = set(
            Habit.objects.filter(user=request.user).values_list("id", flat=True)
        )
        if len(set(ordered_ids)) != len(ordered_ids) or (
            set(ordered_ids) - user_habit_ids
        ):
            return JsonResponse({"ok": False, "error": "Habit set mismatch."}, status=400)

        for index, habit_id in enumerate(ordered_ids):
            Habit.objects.filter(id=habit_id, user=request.user).update(sort_order=index + 1)
    except Exception:
        logger.exception(
            "reorder_habits failed for user_id=%s",
            getattr(request.user, "id", None),
        )
        return JsonResponse(
            {"ok": False, "error": "Could not save the new order. Please try again."},
            status=500,
        )

    return JsonResponse({"ok": True})


@login_required
def reports(request):
    today = timezone.localdate()
    habits = list(
        Habit.objects.filter(user=request.user)
        .prefetch_related("categories", "pauses", "plan_versions__categories")
        .order_by("sort_order", "name")
    )

    weekly_reports = build_weekly_reports(habits, weeks=8, today=today)
    monthly_reports = build_monthly_reports(habits, months=6, today=today)

    weekly_labels = [item["label"] for item in weekly_reports]
    weekly_rates = []
    weekly_streak = []
    for item in weekly_reports:
        if item["total_scheduled"] == 0:
            weekly_rates.append(None)
            weekly_streak.append(None)
        else:
            weekly_rates.append(item["completion_rate"])
            weekly_streak.append(item["avg_current_streak"])
    streak_values = [value for value in weekly_streak if value is not None]
    weekly_streak_max = max(1, ceil(max(streak_values))) if streak_values else 1

    monthly_labels = [item["label"] for item in monthly_reports]
    monthly_rates = []
    monthly_consistency = []
    for item in monthly_reports:
        if item["total_scheduled"] == 0:
            monthly_rates.append(None)
            monthly_consistency.append(None)
        else:
            monthly_rates.append(item["completion_rate"])
            monthly_consistency.append(item["consistency_score"])

    weekly_reports_desc = list(reversed(weekly_reports))
    monthly_reports_desc = list(reversed(monthly_reports))

    context = {
        "today": today,
        "weekly_reports": weekly_reports,
        "weekly_reports_desc": weekly_reports_desc,
        "monthly_reports": monthly_reports,
        "monthly_reports_desc": monthly_reports_desc,
        "weekly_labels": json.dumps(weekly_labels),
        "weekly_rates": json.dumps(weekly_rates),
        "weekly_streak": json.dumps(weekly_streak),
        "weekly_streak_max": weekly_streak_max,
        "monthly_labels": json.dumps(monthly_labels),
        "monthly_rates": json.dumps(monthly_rates),
        "monthly_consistency": json.dumps(monthly_consistency),
        "analytics_cutoff_note": ANALYTICS_CUTOFF_NOTE,
    }
    return render(request, "habits/reports.html", context)


@login_required
def habit_compare(request):
    habits = list(
        Habit.objects.filter(user=request.user)
        .prefetch_related("categories", "pauses", "plan_versions__categories")
        .order_by("sort_order", "name")
    )
    today = timezone.localdate()
    # Both comparison windows are analytics: they end at yesterday and keep
    # their full length by moving the start back with the end.
    window_start, window_end = analytics_window(90, today)
    last30_start, last30_end = analytics_window(30, today)
    habits_by_id = {habit.id: habit for habit in habits}

    selected_ids = []
    for raw_id in request.GET.getlist("habit_ids"):
        try:
            habit_id = int(raw_id)
        except (TypeError, ValueError):
            continue
        if habit_id in habits_by_id and habit_id not in selected_ids:
            selected_ids.append(habit_id)
    selected_ids = selected_ids[:4]
    selected_habits = [habits_by_id[habit_id] for habit_id in selected_ids]

    comparison_rows = []
    chart_payload = {"last30": {"labels": [], "datasets": []}, "all": {"labels": [], "datasets": []}}
    if selected_habits:
        chart_payload = _build_compare_chart_payload(selected_habits, today)

    if len(selected_habits) >= 2:
        for habit in selected_habits:
            metrics_90 = habit_performance_metrics(habit, window_start, window_end)
            metrics_30 = habit_performance_metrics(habit, last30_start, last30_end)
            all_time_start = habit_tracking_start(habit)
            metrics_all = habit_performance_metrics(
                habit,
                all_time_start,
                window_end,
            )

            comparison_rows.append(
                {
                    "habit": habit,
                    "metrics_90": metrics_90,
                    "metrics_30": metrics_30,
                    "metrics_all": metrics_all,
                }
            )

    context = {
        "habits": habits,
        "selected_ids": selected_ids,
        "selected_habits": selected_habits,
        "comparison_rows": comparison_rows,
        "compare_limit": 4,
        "chart_payload": json.dumps(chart_payload),
        "window_start": window_start,
        "today": today,
        "analytics_cutoff_note": ANALYTICS_CUTOFF_NOTE,

    }
    return render(request, "habits/habit_compare.html", context)


def _build_compare_chart_payload(selected_habits, today):
    # Compare charts are analytics, so every series ends at the finalized
    # cutoff. Labels come from the same range, so no chart prints today's
    # date next to a point that was deliberately excluded.
    end_date = analytics_end_date(today)

    def _build_range(start_date):
        if start_date > end_date:
            return [end_date]
        days = (end_date - start_date).days + 1
        return [start_date + timedelta(days=offset) for offset in range(days)]

    def _build_timeframe_series(start_date):
        labels = [day.strftime("%b %d") for day in _build_range(start_date)]
        datasets = []
        for habit in selected_habits:
            completion_map, _ = get_completion_maps(habit, start_date, end_date)
            occurrences = {
                occurrence.date: occurrence
                for occurrence in iter_scheduled_occurrences(
                    habit,
                    start_date,
                    end_date,
                )
            }

            tracking_start = habit_tracking_start(habit)
            weighted_completion_total = 0.0
            priority_weight_total = 0.0
            points = []
            for day in _build_range(start_date):
                if day < tracking_start:
                    points.append(None)
                    continue
                occurrence = occurrences.get(day)
                if occurrence is not None:
                    priority_weight = occurrence.priority_weight
                    weighted_completion_total += (
                        completion_map.get(day, 0.0) * priority_weight
                    )
                    priority_weight_total += priority_weight
                average_value = (
                    weighted_completion_rate_from_totals(
                        weighted_completion_total,
                        priority_weight_total,
                    )
                    if priority_weight_total
                    else None
                )
                points.append(average_value)
            datasets.append({"label": habit.name, "data": points})
        return {"labels": labels, "datasets": datasets}

    last30_start, _ = analytics_window(30, today)
    all_time_start = min(
        habit_tracking_start(habit)
        for habit in selected_habits
    )
    return {
        "last30": _build_timeframe_series(last30_start),
        "all": _build_timeframe_series(all_time_start),
    }



def _previous_period_for_window(window_start, window_end):
    window_days = (window_end - window_start).days + 1
    previous_end = window_start - timedelta(days=1)
    previous_start = previous_end - timedelta(days=window_days - 1)
    if previous_start > previous_end:
        return None, None
    return previous_start, previous_end


def _format_period_label(start_date, end_date):
    if not start_date or not end_date:
        return ""
    if start_date == end_date:
        return start_date.strftime("%b %d")
    return f"{start_date.strftime('%b %d')} - {end_date.strftime('%b %d')}"


def _build_score_driver_cards(score_drivers):
    card_specs = (
        {
            "title": "Biggest score booster",
            "key": "booster",
            "value_key": "impact_points",
            "value_label": "weighted share",
            "empty": "No scored habits yet.",
            "help_text": (
                "This habit is shown because it adds the most to your overall Consistency Score. "
                "Its impact grows when it has stronger completion, a higher priority, and more scheduled sessions. "
                "The number is its weighted share of the current overall score."
            ),
        },
        {
            "title": "Biggest score drag",
            "key": "drag",
            "value_key": "drag_points",
            "value_label": "possible score gap",
            "empty": "No scored habits yet.",
            "help_text": (
                "This habit is shown because it is lowering your score the most. "
                "Its impact grows when it has lower completion, a higher priority, and more scheduled sessions. "
                "The number is the estimated score gap this habit still has to close."
            ),
        },
        {
            "title": "Most improved",
            "key": "improved",
            "value_key": "score_delta",
            "value_label": "score change",
            "empty": "No score improvement in the comparison window.",
            "show_sign": True,
            "help_text": (
                "The habit with the largest increase in Consistency Score from "
                "the previous comparison window to the current one."
            ),
        },
        {
            "title": "Most declined",
            "key": "declined",
            "value_key": "score_delta",
            "value_label": "score change",
            "empty": "No score decline in the comparison window.",
            "show_sign": True,
            "help_text": (
                "The habit with the largest decrease in Consistency Score from "
                "the previous comparison window to the current one."
            ),
        },
    )
    cards = []
    for spec in card_specs:
        driver = score_drivers.get(spec["key"])
        cards.append(
            {
                "title": spec["title"],
                "driver": driver,
                "value": driver[spec["value_key"]] if driver else None,
                "value_label": spec["value_label"],
                "empty": spec["empty"],
                "show_sign": spec.get("show_sign", False),
                "help_text": spec["help_text"],
            }
        )
    return cards


@login_required
def profile(request):
    return redirect("habits:user_profile", username=request.user.username)


@login_required
def username_profile(request, username):
    User = get_user_model()
    profile_user = get_object_or_404(User, username=username)
    context = _build_profile_context(profile_user, request.user)
    return render(request, "habits/profile.html", context)


def _build_profile_context(profile_user, current_user):
    today = timezone.localdate()
    is_own_profile = profile_user == current_user
    friend_request = None
    friend_status = "none" if not is_own_profile else "self"
    progress_sharing = None
    progress_sharing_state = "unavailable"
    yesterday_progress = None
    if not is_own_profile:
        friend_request = _friend_request_between(profile_user, current_user)
        if friend_request:
            if friend_request.status == FriendRequest.STATUS_ACCEPTED:
                friend_status = "accepted"
            elif friend_request.to_user_id == current_user.id:
                friend_status = "incoming_pending"
            else:
                friend_status = "outgoing_pending"

    if friend_status == "accepted":
        progress_sharing_state = "none"
        progress_sharing = (
            ProgressSharing.between(profile_user, current_user)
            .filter(friendship=friend_request)
            .first()
        )
        if progress_sharing is not None:
            if progress_sharing.status == ProgressSharing.STATUS_PENDING:
                progress_sharing_state = (
                    "outgoing_pending"
                    if progress_sharing.requester_id == current_user.id
                    else "incoming_pending"
                )
            elif progress_sharing.status == ProgressSharing.STATUS_ACTIVE:
                yesterday_progress = get_shared_yesterday_progress(
                    current_user,
                    profile_user,
                    today=today,
                )
                if yesterday_progress is not None:
                    progress_sharing_state = "active"
    can_view_analytics = is_own_profile or friend_status == "accepted"
    metrics = None
    monthly_reports = []
    monthly_history_reports = []
    daily_labels = []
    daily_rates = []
    progress_labels = []
    progress_rates = []
    if can_view_analytics:
        metrics = _build_user_metrics(profile_user, today)

        monthly_reports = build_monthly_reports(metrics["habits"], months=12, today=today)
        progress_labels = [item["label"] for item in monthly_reports]
        progress_rates = [
            item["completion_rate"] if item["total_scheduled"] else None
            for item in monthly_reports
        ]
        monthly_history_reports = list(reversed(monthly_reports))

        # Profile charts use finalized days only, so today is never shown as a
        # point whose data was excluded.
        daily_start, daily_end = analytics_window(15, today)
        daily_series = daily_average_completion_series(
            metrics["habits"],
            daily_start,
            daily_end,
        )
        daily_labels = [item["label"] for item in daily_series]
        daily_rates = [item["value"] for item in daily_series]

    context = {
        "profile_user": profile_user,
        "is_own_profile": is_own_profile,
        "can_view_analytics": can_view_analytics,
        "today": today,
        "total_habits": metrics["total_habits"] if metrics else None,
        "overall_completion": metrics["overall_completion"] if metrics else None,
        "best_streak": metrics["best_streak"] if metrics else None,
        "consistency_score": metrics["consistency_score"] if metrics else None,
        "total_scheduled": metrics["total_scheduled"] if metrics else None,
        "total_completed": metrics["total_completed"] if metrics else None,
        "monthly_reports": monthly_reports,
        "monthly_history_reports": monthly_history_reports,
        "progress_labels": json.dumps(progress_labels),
        "progress_rates": json.dumps(progress_rates),
        "daily_labels": json.dumps(daily_labels),
        "daily_rates": json.dumps(daily_rates),
        "friend_request": friend_request,
        "friend_status": friend_status,
        "progress_sharing": progress_sharing,
        "progress_sharing_state": progress_sharing_state,
        "yesterday_progress": yesterday_progress,
        "analytics_cutoff_note": ANALYTICS_CUTOFF_NOTE,
    }
    return context


def _build_user_metrics(user, today, start_date=None):
    habits = list(
        Habit.objects.filter(user=user)
        .prefetch_related("categories", "pauses", "plan_versions__categories")
        .order_by("sort_order", "name")
    )

    if start_date is None and habits:
        start_date = min(habit_tracking_start(habit) for habit in habits)
    elif start_date is None:
        start_date = today

    aggregated = compute_user_metrics(habits, start_date, today)
    total_habits = sum(
        1 for habit in habits if not habit.is_paused and not habit.is_archived
    )
    aggregate = aggregated["aggregate"]


    return {
        "habits": habits,
        "start_date": start_date,
        "total_habits": total_habits,
        "overall_completion": aggregate["completion_rate"],
        "best_streak": aggregated["best_streak"],
        "consistency_score": aggregated["consistency_score"],
        "total_scheduled": aggregate["total_scheduled"],
        "total_completed": aggregate["total_completed"],
    }


def _user_tracking_start(user, today):
    """Return the first date a user ever had a habit scheduled."""
    starts = [
        habit_tracking_start(habit)
        for habit in Habit.objects.filter(user=user).prefetch_related(
            "plan_versions__categories"
        )
    ]
    return min(starts) if starts else today


@login_required
def leaderboard(request):
    today = timezone.localdate()
    requested_window = request.GET.get("window")
    leaderboard_window = "all" if requested_window == "all" else "current"

    participants = [request.user] + _accepted_friends_for(request.user)

    # Ranking is analytics, so both windows end at the finalized cutoff. The
    # 30-day window keeps its full length by moving the start back with the
    # end, and the displayed range shows the same dates that were scored.
    window_end = analytics_end_date(today)

    if leaderboard_window == "current":
        window_start, window_end = analytics_window(30, today)
        window_label = "Last 30 days"
        window_range = (
            f"{window_start.strftime('%b %d')} - {window_end.strftime('%b %d')}"
        )
        window_title = "Current window"
    else:

        # Every participant is measured over one identical calendar window.
        # Previously each user was scored from their own first tracked date, so
        # the column compared a 3-day record against a 2-year record and called
        # it the same metric. The shared window starts at the earliest date any
        # participant began tracking; users who joined later simply have fewer
        # scheduled sessions inside it, which the evidence adjustment handles.
        participant_starts = [
            _user_tracking_start(user, today) for user in participants
        ]
        window_start = min(participant_starts) if participant_starts else window_end
        window_label = "All tracked history"
        window_range = (
            f"{window_start.strftime('%b %d, %Y')} - "
            f"{window_end.strftime('%b %d, %Y')}"
        )
        window_title = "All time"

    entries = []

    for user in participants:
        # Scoring every participant to ``window_end`` keeps today's sessions out
        # of both the score and the evidence count that weights the ranking.
        metrics = _build_user_metrics(user, window_end, start_date=window_start)
        total_scheduled = metrics["total_scheduled"]
        entries.append(
            {
                "user": user,
                "is_current_user": user == request.user,
                "total_habits": metrics["total_habits"],
                "overall_completion": metrics["overall_completion"],
                "best_streak": metrics["best_streak"],
                "consistency_score": metrics["consistency_score"],
                "total_scheduled": total_scheduled,
                "total_completed": metrics["total_completed"],
                # Ranking uses an evidence-adjusted score so a handful of
                # perfect sessions cannot outrank sustained consistency. The
                # displayed consistency_score stays untouched.
                "ranking_score": leaderboard_ranking_score(
                    metrics["consistency_score"],
                    total_scheduled,
                ),
                "has_full_evidence": total_scheduled
                >= LEADERBOARD_CONFIDENCE_SESSIONS,
            }
        )

    entries.sort(
        key=lambda entry: (
            -entry["ranking_score"],
            -entry["consistency_score"],
            -entry["overall_completion"],
            -entry["total_completed"],
            entry["user"].username.lower(),
        )
    )
    for rank, entry in enumerate(entries, start=1):
        entry["rank"] = rank

    current_entry = next(
        (entry for entry in entries if entry["is_current_user"]),
        None,
    )
    leader_entry = entries[0] if entries else None

    return render(
        request,
        "habits/leaderboard.html",
        {
            "today": today,
            "leaderboard_entries": entries,
            "friend_count": len(participants) - 1,
            "current_entry": current_entry,
            "leader_entry": leader_entry,
            "leaderboard_window": leaderboard_window,
            "leaderboard_window_label": window_label,
            "leaderboard_window_range": window_range,
            "leaderboard_window_title": window_title,
            "leaderboard_window_start": window_start,
            "leaderboard_window_end": window_end,
            "leaderboard_min_sessions": LEADERBOARD_CONFIDENCE_SESSIONS,
            "analytics_cutoff_note": ANALYTICS_CUTOFF_NOTE,
        },
    )


@login_required
def user_search(request):
    query = (request.GET.get("q") or "").strip()
    results = []

    if query:
        User = get_user_model()
        users = list(
            User.objects.filter(username__icontains=query)
            .exclude(id=request.user.id)
            .order_by("username")[:20]
        )
        friend_states = _friend_request_states(request.user, users)
        for user in users:
            state = friend_states.get(
                user.id,
                {"status": "none", "friend_request": None},
            )
            results.append(
                {
                    "user": user,
                    "status": state["status"],
                    "friend_request": state["friend_request"],
                }
            )

    return render(
        request,
        "habits/user_search.html",
        {
            "query": query,
            "results": results,
        },
    )


@login_required
@require_POST
def send_friend_request(request, user_id):
    User = get_user_model()
    target_user = get_object_or_404(User, id=user_id)
    next_url = _safe_next_url(request)

    if target_user == request.user:
        messages.error(request, "You cannot send a friend request to yourself.")
        return redirect(next_url or reverse("habits:user_search"))

    existing_request = _friend_request_between(request.user, target_user)
    if existing_request:
        if existing_request.status == FriendRequest.STATUS_ACCEPTED:
            messages.info(request, f"You and {target_user.username} are already friends.")
        elif existing_request.to_user_id == request.user.id:
            existing_request.accept()
            messages.success(request, f"You accepted {target_user.username}'s friend request.")
        else:
            messages.info(request, f"Friend request already sent to {target_user.username}.")
        return redirect(
            next_url
            or f"{reverse('habits:user_search')}?q={target_user.username}"
        )

    try:
        FriendRequest.objects.create(from_user=request.user, to_user=target_user)
        messages.success(request, f"Friend request sent to {target_user.username}.")
    except IntegrityError:
        messages.info(request, "A friend request already exists for this user.")

    return redirect(
        next_url
        or f"{reverse('habits:user_search')}?q={target_user.username}"
    )


@login_required
@require_POST
def accept_friend_request(request, request_id):
    friend_request = get_object_or_404(
        FriendRequest,
        id=request_id,
        to_user=request.user,
        status=FriendRequest.STATUS_PENDING,
    )
    friend_request.accept()
    messages.success(
        request,
        f"You accepted {friend_request.from_user.username}'s friend request.",
    )
    return redirect(_safe_next_url(request) or reverse("habits:user_search"))


@login_required
@require_POST
def cancel_friend_request(request, request_id):
    friend_request = get_object_or_404(
        FriendRequest,
        id=request_id,
        from_user=request.user,
        status=FriendRequest.STATUS_PENDING,
    )
    other_user = friend_request.to_user
    friend_request.delete()
    messages.success(request, f"Friend request to {other_user.username} canceled.")
    return redirect(_safe_next_url(request) or reverse("habits:user_search"))


@login_required
@require_POST
def request_progress_sharing(request, user_id):
    User = get_user_model()
    target_user = get_object_or_404(User, id=user_id)
    next_url = _safe_next_url(request)
    fallback_url = reverse(
        "habits:user_profile",
        args=[target_user.username],
    )

    if target_user == request.user:
        messages.error(request, "You cannot share progress with yourself.")
        return redirect(next_url or fallback_url)

    friendship = _friend_request_between(request.user, target_user)
    if friendship is None or friendship.status != FriendRequest.STATUS_ACCEPTED:
        messages.error(request, "Only friends can request Progress Sharing.")
        return redirect(next_url or fallback_url)

    outcome = "already_pending"
    try:
        with transaction.atomic():
            locked_friendship = (
                FriendRequest.objects.select_for_update()
                .filter(
                    pk=friendship.pk,
                    status=FriendRequest.STATUS_ACCEPTED,
                )
                .first()
            )
            if locked_friendship is None:
                outcome = "not_friends"
            else:
                existing = ProgressSharing.objects.filter(
                    friendship=locked_friendship
                ).first()
                if existing is None:
                    ProgressSharing.objects.create(
                        friendship=locked_friendship,
                        user_one=request.user,
                        user_two=target_user,
                        requester=request.user,
                    )
                    outcome = "sent"
                elif existing.status == ProgressSharing.STATUS_ACTIVE:
                    outcome = "already_active"
    except IntegrityError:
        # The unique friendship/pair constraints resolve simultaneous requests
        # into one row.  Once the competing transaction commits, report the
        # resulting state instead of surfacing a server error.
        existing = ProgressSharing.objects.filter(friendship=friendship).first()
        outcome = (
            "already_active"
            if existing and existing.status == ProgressSharing.STATUS_ACTIVE
            else "already_pending"
        )

    if outcome == "sent":
        messages.success(request, "Progress sharing request sent.")
    elif outcome == "already_active":
        messages.info(request, "Progress Sharing is already active with this friend.")
    elif outcome == "not_friends":
        messages.error(request, "Only friends can request Progress Sharing.")
    else:
        messages.info(request, "A Progress Sharing request is already pending.")
    return redirect(next_url or fallback_url)


@login_required
@require_POST
def accept_progress_sharing(request, sharing_id):
    next_url = _safe_next_url(request)
    with transaction.atomic():
        sharing = get_object_or_404(
            ProgressSharing.objects.select_for_update()
            .select_related("friendship", "user_one", "user_two")
            .filter(Q(user_one=request.user) | Q(user_two=request.user))
            .exclude(requester=request.user),
            pk=sharing_id,
            status=ProgressSharing.STATUS_PENDING,
        )
        friendship_still_active = FriendRequest.objects.select_for_update().filter(
            pk=sharing.friendship_id,
            status=FriendRequest.STATUS_ACCEPTED,
        ).exists()
        if not friendship_still_active:
            sharing.delete()
            messages.error(request, "Progress Sharing requires an active friendship.")
        else:
            sharing.accept()
            messages.success(
                request,
                f"You and {sharing.requester.username} are now sharing yesterday's progress.",
            )
    return redirect(next_url or reverse("habits:user_search"))


@login_required
@require_POST
def decline_progress_sharing(request, sharing_id):
    next_url = _safe_next_url(request)
    with transaction.atomic():
        sharing = get_object_or_404(
            ProgressSharing.objects.select_for_update()
            .filter(Q(user_one=request.user) | Q(user_two=request.user))
            .exclude(requester=request.user),
            pk=sharing_id,
            status=ProgressSharing.STATUS_PENDING,
        )
        sharing.delete()
    messages.success(request, "Progress Sharing request declined.")
    return redirect(next_url or reverse("habits:user_search"))


@login_required
@require_POST
def cancel_progress_sharing(request, sharing_id):
    next_url = _safe_next_url(request)
    with transaction.atomic():
        sharing = get_object_or_404(
            ProgressSharing.objects.select_for_update(),
            pk=sharing_id,
            requester=request.user,
            status=ProgressSharing.STATUS_PENDING,
        )
        sharing.delete()
    messages.success(request, "Progress Sharing request canceled.")
    return redirect(next_url or reverse("habits:user_search"))


@login_required
@require_POST
def stop_progress_sharing(request, sharing_id):
    next_url = _safe_next_url(request)
    with transaction.atomic():
        sharing = get_object_or_404(
            ProgressSharing.objects.select_for_update().filter(
                Q(user_one=request.user) | Q(user_two=request.user)
            ),
            pk=sharing_id,
            status=ProgressSharing.STATUS_ACTIVE,
        )
        other_user = (
            sharing.user_two
            if sharing.user_one_id == request.user.id
            else sharing.user_one
        )
        sharing.delete()
    messages.success(
        request,
        f"Progress Sharing with {other_user.username} has stopped.",
    )
    return redirect(next_url or reverse("habits:user_search"))


@login_required
@require_POST
def remove_friend(request, request_id):
    friend_request = get_object_or_404(
        FriendRequest,
        id=request_id,
        status=FriendRequest.STATUS_ACCEPTED,
    )
    if request.user not in (friend_request.from_user, friend_request.to_user):
        messages.error(request, "You cannot modify that friendship.")
        return redirect(_safe_next_url(request) or reverse("habits:user_search"))

    other_user = (
        friend_request.to_user
        if friend_request.from_user_id == request.user.id
        else friend_request.from_user
    )
    # ProgressSharing has a database CASCADE to this friendship, so pending and
    # active access disappear atomically with the friendship.
    with transaction.atomic():
        friend_request.delete()
    messages.success(request, f"You and {other_user.username} are no longer friends.")
    return redirect(_safe_next_url(request) or reverse("habits:user_search"))


@login_required
@require_POST
def daily_recap(request):
    today = timezone.localdate()
    target_date = daily_recap_target_date(today)
    session_date_value = request.session.get("daily_recap_date")
    posted_date = _parse_date_value(request.POST.get("date"))

    if (
        session_date_value != target_date.isoformat()
        or posted_date != target_date
    ):
        request.session.pop("daily_recap_date", None)
        messages.error(request, "That daily recap has expired. Refresh the page to continue.")
        return redirect(_safe_next_url(request) or reverse("habits:today"))

    pending = get_pending_habits_for_date(request.user, target_date)
    if not pending:
        request.session.pop("daily_recap_date", None)
        mark_daily_recap_completed(request.user, target_date)
        return redirect(_safe_next_url(request) or reverse("habits:today"))

    updates = []
    errors = []

    for item in pending:
        habit = item["habit"]
        effective_habit_type = item.get("habit_type", habit.habit_type)
        effective_target_value = item.get("target_value", habit.target_value)
        if effective_habit_type == Habit.HABIT_BINARY:
            is_done = request.POST.get(f"completed_{habit.id}") == "1"
            completion_percentage = Decimal("100") if is_done else Decimal("0")
            raw_value = completion_percentage
        elif effective_habit_type == Habit.HABIT_PARTIAL:
            raw_value = request.POST.get(f"percentage_{habit.id}")
            if raw_value in (None, ""):
                errors.append(habit.name)
                continue
            completion_percentage = _clamp_percentage(raw_value)
            raw_value = completion_percentage
        else:
            if not effective_target_value or effective_target_value <= 0:
                errors.append(habit.name)
                continue
            raw_value = request.POST.get(f"value_{habit.id}")
            if raw_value in (None, ""):
                errors.append(habit.name)
                continue
            target_value = effective_target_value.quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
            raw_value = _parse_decimal(raw_value) or Decimal("0")
            raw_value = raw_value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

            if raw_value < 0:
                raw_value = Decimal("0")
            if raw_value > target_value:
                raw_value = target_value
            completion_percentage = _percentage_from_value(raw_value, target_value)

        updates.append((habit, completion_percentage, raw_value))

    if errors:
        messages.error(request, "Please fill in all pending habits before continuing.")
        return redirect(_safe_next_url(request) or reverse("habits:today"))

    try:
        with transaction.atomic():
            for habit, completion_percentage, raw_value in updates:
                completion, _ = HabitCompletion.objects.get_or_create(
                    habit=habit,
                    date=target_date,
                )
                completion.completion_percentage = completion_percentage
                completion.raw_value = raw_value
                completion.save(update_fields=["completion_percentage", "raw_value"])

            # Record the finished recap in the same transaction as the completions.
            # "Save and continue" may legitimately persist progress below 100%, so
            # this row, not the pending-habit check, is what stops the prompt from
            # reappearing on other devices, browsers, and sessions.
            mark_daily_recap_completed(request.user, target_date)
    except Exception:
        logger.exception(
            "daily_recap failed for user_id=%s on %s",
            getattr(request.user, "id", None),
            target_date,
        )
        messages.error(
            request,
            "Could not save your recap right now. Please try again.",
        )
        return redirect(_safe_next_url(request) or reverse("habits:today"))

    request.session.pop("daily_recap_date", None)
    return redirect(_safe_next_url(request) or reverse("habits:today"))





def signup(request):
    if request.method == "POST":
        form = UserCreationForm(request.POST)
        if form.is_valid():
            user = form.save()
            login(request, user)
            messages.success(request, "Signup successful. Welcome to Consistify.")
            return redirect("habits:today")
    else:
        form = UserCreationForm()

    return render(request, "registration/signup.html", {"form": form})


def _friend_request_between(first_user, second_user):
    friendship_key = FriendRequest.build_friendship_key(first_user.id, second_user.id)
    return FriendRequest.objects.filter(friendship_key=friendship_key).first()


def _accepted_friends_for(user):
    accepted_requests = (
        FriendRequest.objects.filter(status=FriendRequest.STATUS_ACCEPTED)
        .filter(Q(from_user=user) | Q(to_user=user))
        .select_related("from_user", "to_user")
    )

    friends = []
    seen_user_ids = set()
    for friend_request in accepted_requests:
        friend = (
            friend_request.to_user
            if friend_request.from_user_id == user.id
            else friend_request.from_user
        )
        if friend.id in seen_user_ids:
            continue
        seen_user_ids.add(friend.id)
        friends.append(friend)
    return sorted(friends, key=lambda friend: friend.username.lower())


def _friend_request_states(current_user, users):
    user_ids = [user.id for user in users]
    if not user_ids:
        return {}

    requests = FriendRequest.objects.filter(
        Q(from_user=current_user, to_user_id__in=user_ids)
        | Q(to_user=current_user, from_user_id__in=user_ids)
    )

    states = {}
    priorities = {
        "none": 0,
        "outgoing_pending": 1,
        "incoming_pending": 2,
        "accepted": 3,
    }
    for friend_request in requests:
        other_user_id = (
            friend_request.to_user_id
            if friend_request.from_user_id == current_user.id
            else friend_request.from_user_id
        )
        if friend_request.status == FriendRequest.STATUS_ACCEPTED:
            status = "accepted"
        elif friend_request.to_user_id == current_user.id:
            status = "incoming_pending"
        else:
            status = "outgoing_pending"

        current_state = states.get(other_user_id, {"status": "none"})
        if priorities[status] > priorities[current_state["status"]]:
            states[other_user_id] = {
                "status": status,
                "friend_request": friend_request,
            }
    return states


def _safe_next_url(request):
    next_url = request.POST.get("next") or request.META.get("HTTP_REFERER")
    if next_url and url_has_allowed_host_and_scheme(
        next_url,
        allowed_hosts={request.get_host()},
    ):
        return next_url
    return None


def _progress_error_response(request, message):
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return JsonResponse({"ok": False, "error": message}, status=400)
    messages.error(request, message)
    return redirect(_safe_next_url(request) or reverse("habits:today"))


def _wants_json(request):
    """Return True when the caller prefers JSON over an HTML redirect.

    Used by error helpers to decide whether to return a JSON envelope or to
    fall back to the standard messages framework + redirect flow.
    """
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return True
    accept = request.headers.get("Accept", "")
    return "application/json" in accept and "text/html" not in accept


def _parse_decimal(raw_value):
    try:
        return Decimal(str(raw_value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _clamp_percentage(raw_value):
    value = _parse_decimal(raw_value)
    if value is None:
        return Decimal("0")
    if value < 0:
        value = Decimal("0")
    if value > 100:
        value = Decimal("100")
    return value.quantize(Decimal("1"), rounding=ROUND_HALF_UP)


def _percentage_from_value(current_value, target_value):
    if not target_value or target_value <= 0:
        return Decimal("0")
    percentage = (current_value / target_value) * Decimal("100")
    if percentage < 0:
        percentage = Decimal("0")
    if percentage > 100:
        percentage = Decimal("100")
    return percentage.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _get_date_from_request(request):
    raw_date = request.GET.get("date") or request.POST.get("date")
    parsed = _parse_date_value(raw_date)
    if parsed:
        return parsed
    return timezone.localdate()


def _parse_date_value(raw_date):
    if not raw_date:
        return None
    try:
        return parse_date(raw_date)
    except (TypeError, ValueError):
        return None


def _extract_authorization_token(authorization_header):
    if not authorization_header:
        return ""

    if authorization_header.startswith("Bearer "):
        return authorization_header.removeprefix("Bearer ").strip()

    return authorization_header.strip()


def _run_cron_job():
    """Placeholder for cron logic that can be extended later."""
    return None
