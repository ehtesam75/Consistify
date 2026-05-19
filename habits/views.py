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
from django.db import IntegrityError, transaction
from django.db.models import Max, Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .forms import HabitForm
from .models import FriendRequest, Habit, HabitCompletion, HabitPause
from .services import (
    build_category_analytics,
    build_habit_score_drivers,
    build_monthly_reports,
    build_overall_score_breakdown,
    build_weekly_reports,
    calculate_overall_consistency,
    calculate_streaks,
    completion_stats,
    get_completion_maps,
    get_next_scheduled_date,
    habit_performance_metrics,
    iter_scheduled_dates,
)


logger = logging.getLogger(__name__)


def index(request):
    if request.user.is_authenticated:
        return redirect("habits:today")
    return render(request, "habits/home.html")


class ConsistifyLoginView(auth_views.LoginView):
    template_name = "registration/login.html"

    def form_valid(self, form):
        response = super().form_valid(form)
        messages.success(self.request, "Logged in successfully.")
        return response


class ConsistifyLogoutView(auth_views.LogoutView):
    next_page = "habits:index"

    def post(self, request, *args, **kwargs):
        response = super().post(request, *args, **kwargs)
        messages.success(request, "Logged out successfully.")
        return response


@login_required
def habit_list(request):
    today = timezone.localdate()
    target_date = _get_date_from_request(request)
    habits = list(
        Habit.objects.filter(user=request.user)
        .prefetch_related("categories", "pauses")
        .order_by("sort_order", "name")
    )
    active_habits = [habit for habit in habits if not habit.is_paused_on(target_date)]
    completions = HabitCompletion.objects.filter(habit__in=habits, date=target_date)
    completion_map = {completion.habit_id: completion for completion in completions}

    scheduled_habits = []
    for habit in habits:
        if habit.is_scheduled_on(target_date):
            completion = completion_map.get(habit.id)
            completion_percentage = (
                float(completion.completion_percentage) if completion else 0.0
            )
            raw_value = None
            if completion and completion.raw_value is not None:
                raw_value = float(completion.raw_value)
                if habit.habit_type == Habit.HABIT_QUANTITATIVE:
                    raw_value = int(raw_value)
            scheduled_habits.append(
                {
                    "habit": habit,
                    "completed": completion_percentage >= 100,
                    "completion_percentage": completion_percentage,
                    "raw_value": raw_value,
                    "tags": habit.get_tags(),
                }
            )

    completed_count = sum(
        1 for item in scheduled_habits if item["completion_percentage"] >= 100
    )

    hide_completed = request.GET.get("hide_completed") == "1"
    visible_scheduled_habits = (
        [item for item in scheduled_habits if not item["completed"]]
        if hide_completed
        else scheduled_habits
    )

    context = {
        "target_date": target_date,
        "today": today,
        "prev_date": target_date - timedelta(days=1),
        "next_date": target_date + timedelta(days=1),
        "scheduled_habits": visible_scheduled_habits,
        "scheduled_count": len(scheduled_habits),
        "completed_count": completed_count,
        "hide_completed": hide_completed,
        "visible_scheduled_count": len(visible_scheduled_habits),
        "habits": habits,
        "all_count": len(active_habits),
    }
    return render(request, "habits/habit_list.html", context)


@login_required
def dashboard(request):
    today = timezone.localdate()
    window_start = today - timedelta(days=29)
    dashboard_window_note = "Last 30 days"
    previous_window_start, previous_window_end = _previous_period_for_window(
        window_start,
        today,
    )
    habits = list(
        Habit.objects.filter(user=request.user)
        .prefetch_related("categories", "pauses")
        .order_by("sort_order", "name")
    )
    total_habits = sum(1 for habit in habits if not habit.is_paused)

    habit_cards = []
    total_scheduled = 0
    total_completion = 0.0
    total_completed = 0

    for habit in habits:
        metrics = habit_performance_metrics(habit, window_start, today)
        total_scheduled += metrics["scheduled_total"]
        total_completion += metrics["completion_rate"] * metrics["scheduled_total"]
        total_completed += metrics["completed_total"]
        habit_cards.append(
            {
                "habit": habit,
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

    overall_rate = round(total_completion / total_scheduled, 1) if total_scheduled else 0.0
    score_breakdown = build_overall_score_breakdown(
        habits,
        window_start,
        today,
        previous_window_start,
        previous_window_end,
    )
    score_breakdown["current_period_label"] = _format_period_label(window_start, today)
    score_breakdown["previous_period_label"] = (
        _format_period_label(previous_window_start, previous_window_end)
        if previous_window_start and previous_window_end
        else ""
    )
    overall_consistency = score_breakdown["current_score"]
    score_drivers = build_habit_score_drivers(
        habits,
        window_start,
        today,
        previous_window_start,
        previous_window_end,
    )
    category_analytics = build_category_analytics(habits, window_start, today)

    doing_well = [card for card in habit_cards if card["scheduled"] and card["rate"] >= 80]
    needs_focus = [card for card in habit_cards if card["scheduled"] and card["rate"] < 50]

    chart_days = 14
    chart_start = today - timedelta(days=chart_days - 1)
    chart_labels = []
    chart_rates = []
    recent_completions = HabitCompletion.objects.filter(
        habit__in=habits,
        date__range=(chart_start, today),
    )
    completion_map = {
        (completion.habit_id, completion.date): float(completion.completion_percentage or 0)
        for completion in recent_completions
    }

    for offset in range(chart_days):
        current_day = chart_start + timedelta(days=offset)
        daily_values = []
        for habit in habits:
            if not habit.is_scheduled_on(current_day):
                continue
            daily_values.append(completion_map.get((habit.id, current_day), 0.0))
        rate = round(sum(daily_values) / len(daily_values), 1) if daily_values else None
        chart_labels.append(current_day.strftime("%b %d"))
        chart_rates.append(rate)

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
        "dashboard_window_label": _format_period_label(window_start, today),
        "dashboard_window_note": dashboard_window_note,
    }
    return render(request, "habits/dashboard.html", context)


@login_required
def habit_detail(request, habit_id):
    habit = get_object_or_404(
        Habit.objects.prefetch_related("categories", "pauses"),
        id=habit_id,
        user=request.user,
    )
    today = timezone.localdate()
    all_time_start = habit.start_date
    history_dates = list(iter_scheduled_dates(habit, all_time_start, today))
    recent_history_dates = history_dates[-15:]
    chart_dates = history_dates[-15:]

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

    window_start = today - timedelta(days=29)
    window_completion_map = {
        date: value for date, value in completion_map.items() if date >= window_start
    }
    window_value_map = {
        date: value for date, value in value_map.items() if date >= window_start
    }
    stats = completion_stats(
        habit,
        window_start,
        today,
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
        if habit.habit_type == Habit.HABIT_QUANTITATIVE:
            today_raw_value = int(today_raw_value)
    today_completed = today_completion_percentage >= 100
    is_scheduled_today = habit.is_scheduled_on(today)
    next_due = get_next_scheduled_date(habit, today)

    chart_labels = json.dumps([date.strftime("%b %d") for date in chart_dates])
    chart_percentages = json.dumps([completion_map.get(date, 0) for date in chart_dates])

    context = {
        "habit": habit,
        "history": history,
        "stats": stats,
        "all_time_stats": all_time_stats,
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
        "detail_window_note": "Last 30 days",
        "all_time_label": f"Since {all_time_start.strftime('%b %d, %Y')}",
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
            form.save()
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
def pause_habit(request, habit_id):
    habit = get_object_or_404(Habit, id=habit_id, user=request.user)
    today = timezone.localdate()
    start_date = today + timedelta(days=1)
    try:
        with transaction.atomic():
            HabitPause.objects.create(habit=habit, start_date=start_date)
    except IntegrityError:
        messages.info(request, "This habit is already paused or scheduled to pause.")
    else:
        messages.success(request, f'"{habit.name}" paused starting tomorrow.')

    return redirect(_safe_next_url(request) or reverse("habits:habit_detail", args=[habit.id]))


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


@login_required
@require_POST
def update_progress(request, habit_id):
    habit = get_object_or_404(Habit, id=habit_id, user=request.user)
    target_date = _get_date_from_request(request)

    if habit.is_paused_on(target_date):
        messages.error(request, "This habit is paused for that day.")
        next_url = request.POST.get("next") or request.META.get("HTTP_REFERER")
        return redirect(next_url or reverse("habits:today"))

    if not habit.is_scheduled_on(target_date):
        messages.error(request, "This habit is not scheduled for that day.")
        return redirect("habits:today")

    completion, _ = HabitCompletion.objects.get_or_create(
        habit=habit,
        date=target_date,
    )

    completion_percentage = Decimal("0")
    raw_value = None

    if habit.habit_type == Habit.HABIT_BINARY:
        is_done = request.POST.get("completed") is not None
        completion_percentage = Decimal("100") if is_done else Decimal("0")
        raw_value = completion_percentage
    elif habit.habit_type == Habit.HABIT_PARTIAL:
        completion_percentage = _clamp_percentage(request.POST.get("completion_percentage"))
        raw_value = completion_percentage
    else:
        if not habit.target_value or habit.target_value <= 0:
            messages.error(request, "Add a target value before logging progress.")
            next_url = request.POST.get("next") or request.META.get("HTTP_REFERER")
            return redirect(next_url or reverse("habits:today"))
        target_value = habit.target_value.quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        raw_value = _parse_decimal(request.POST.get("current_value")) or Decimal("0")
        raw_value = raw_value.quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        if raw_value < 0:
            raw_value = Decimal("0")
        if raw_value > target_value:
            raw_value = target_value
        completion_percentage = _percentage_from_value(raw_value, target_value)

    completion.completion_percentage = completion_percentage
    completion.raw_value = raw_value
    completion.save(update_fields=["completion_percentage", "raw_value"])

    next_url = request.POST.get("next") or request.META.get("HTTP_REFERER")
    return redirect(next_url or reverse("habits:today"))


@csrf_exempt
def cron_job(request):
    if request.method != "POST":
        return JsonResponse({"ok": False, "error": "Method not allowed."}, status=405)

    authorization_header = request.headers.get("Authorization", "")
    provided_secret = _extract_authorization_token(authorization_header)

    if not settings.CRON_SECRET or not provided_secret or not secrets.compare_digest(
        provided_secret,
        settings.CRON_SECRET,
    ):
        return JsonResponse({"ok": False, "error": "Unauthorized."}, status=401)

    _run_cron_job()
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

    user_habit_ids = list(Habit.objects.filter(user=request.user).values_list("id", flat=True))
    if sorted(ordered_ids) != sorted(user_habit_ids):
        return JsonResponse({"ok": False, "error": "Habit set mismatch."}, status=400)

    for index, habit_id in enumerate(ordered_ids):
        Habit.objects.filter(id=habit_id, user=request.user).update(sort_order=index + 1)

    return JsonResponse({"ok": True})


@login_required
def reports(request):
    today = timezone.localdate()
    habits = list(
        Habit.objects.filter(user=request.user)
        .prefetch_related("categories", "pauses")
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
    }
    return render(request, "habits/reports.html", context)


@login_required
def habit_compare(request):
    habits = list(
        Habit.objects.filter(user=request.user)
        .prefetch_related("categories", "pauses")
        .order_by("sort_order", "name")
    )
    today = timezone.localdate()
    window_start = today - timedelta(days=89)
    last30_start = today - timedelta(days=29)
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
            metrics_90 = habit_performance_metrics(habit, window_start, today)
            metrics_30 = habit_performance_metrics(habit, last30_start, today)
            all_time_start = habit.start_date
            metrics_all = habit_performance_metrics(habit, all_time_start, today)
            comparison_rows.append(
                {
                    "habit": habit,
                    "metrics_90": metrics_90,
                    "metrics_all": metrics_all,
                    "avg_daily_30": round(metrics_30["average_completion"], 1),
                    "avg_daily_all": round(metrics_all["average_completion"], 1),
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
    }
    return render(request, "habits/habit_compare.html", context)


def _build_compare_chart_payload(selected_habits, today):
    def _build_range(start_date):
        if start_date > today:
            return [today]
        days = (today - start_date).days + 1
        return [start_date + timedelta(days=offset) for offset in range(days)]

    def _build_timeframe_series(start_date):
        labels = [day.strftime("%b %d") for day in _build_range(start_date)]
        datasets = []
        for habit in selected_habits:
            completion_map, _ = get_completion_maps(habit, start_date, today)
            running_total = 0.0
            scheduled_count = 0
            points = []
            for day in _build_range(start_date):
                if day < habit.start_date:
                    points.append(None)
                    continue
                if habit.is_scheduled_on(day):
                    running_total += completion_map.get(day, 0.0)
                    scheduled_count += 1
                average_value = round(running_total / scheduled_count, 1) if scheduled_count else None
                points.append(average_value)
            datasets.append({"label": habit.name, "data": points})
        return {"labels": labels, "datasets": datasets}

    last30_start = today - timedelta(days=29)
    all_time_start = min(habit.start_date for habit in selected_habits)
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
            "value_label": "overall score pts",
            "empty": "No scored habits yet.",
        },
        {
            "title": "Biggest score drag",
            "key": "drag",
            "value_key": "drag_points",
            "value_label": "possible score gap",
            "empty": "No scored habits yet.",
        },
        {
            "title": "Most improved",
            "key": "improved",
            "value_key": "score_delta",
            "value_label": "score change",
            "empty": "No score improvement in the comparison window.",
            "show_sign": True,
        },
        {
            "title": "Most declined",
            "key": "declined",
            "value_key": "score_delta",
            "value_label": "score change",
            "empty": "No score decline in the comparison window.",
            "show_sign": True,
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
    metrics = _build_user_metrics(profile_user, today)

    monthly_reports = build_monthly_reports(metrics["habits"], months=12, today=today)
    progress_labels = [item["label"] for item in monthly_reports]
    progress_rates = [
        item["completion_rate"] if item["total_scheduled"] else None
        for item in monthly_reports
    ]
    monthly_history_reports = list(reversed(monthly_reports))

    daily_window_days = 15
    daily_start = today - timedelta(days=daily_window_days - 1)
    daily_labels = []
    daily_rates = []
    completion_map = {}
    if metrics["habits"]:
        recent_completions = HabitCompletion.objects.filter(
            habit__in=metrics["habits"],
            date__range=(daily_start, today),
        )
        completion_map = {
            (completion.habit_id, completion.date): float(
                completion.completion_percentage or 0
            )
            for completion in recent_completions
        }

    for offset in range(daily_window_days):
        current_day = daily_start + timedelta(days=offset)
        daily_values = []
        for habit in metrics["habits"]:
            if not habit.is_scheduled_on(current_day):
                continue
            daily_values.append(completion_map.get((habit.id, current_day), 0.0))
        rate = round(sum(daily_values) / len(daily_values), 1) if daily_values else None
        daily_labels.append(current_day.strftime("%b %d"))
        daily_rates.append(rate)

    context = {
        "profile_user": profile_user,
        "is_own_profile": profile_user == current_user,
        "today": today,
        "total_habits": metrics["total_habits"],
        "overall_completion": metrics["overall_completion"],
        "best_streak": metrics["best_streak"],
        "consistency_score": metrics["consistency_score"],
        "total_scheduled": metrics["total_scheduled"],
        "total_completed": metrics["total_completed"],
        "monthly_reports": monthly_reports,
        "monthly_history_reports": monthly_history_reports,
        "progress_labels": json.dumps(progress_labels),
        "progress_rates": json.dumps(progress_rates),
        "daily_labels": json.dumps(daily_labels),
        "daily_rates": json.dumps(daily_rates),
    }
    return context


def _build_user_metrics(user, today, start_date=None):
    habits = list(
        Habit.objects.filter(user=user)
        .prefetch_related("categories", "pauses")
        .order_by("sort_order", "name")
    )

    if start_date is None and habits:
        start_date = min(habit.start_date for habit in habits)
    elif start_date is None:
        start_date = today

    total_habits = sum(1 for habit in habits if not habit.is_paused)
    total_scheduled = 0
    total_completion = 0.0
    total_completed = 0
    best_streak = 0

    for habit in habits:
        metrics = habit_performance_metrics(habit, start_date, today)
        total_scheduled += metrics["scheduled_total"]
        total_completion += metrics["completion_rate"] * metrics["scheduled_total"]
        total_completed += metrics["completed_total"]
        if metrics["max_streak"] > best_streak:
            best_streak = metrics["max_streak"]

    overall_completion = round(total_completion / total_scheduled, 1) if total_scheduled else 0.0
    consistency_score = calculate_overall_consistency(habits, start_date, today)

    return {
        "habits": habits,
        "start_date": start_date,
        "total_habits": total_habits,
        "overall_completion": overall_completion,
        "best_streak": best_streak,
        "consistency_score": consistency_score,
        "total_scheduled": total_scheduled,
        "total_completed": total_completed,
    }


@login_required
def leaderboard(request):
    today = timezone.localdate()
    requested_window = request.GET.get("window")
    leaderboard_window = "all" if requested_window == "all" else "current"
    if leaderboard_window == "current":
        window_start = today - timedelta(days=29)
        window_label = (
            "Last 30 days· "
            f"{window_start.strftime('%b %d')} - {today.strftime('%b %d')}"
        )
        window_title = "Current window"
    else:
        window_start = None
        window_label = "All tracked history"
        window_title = "All time"

    participants = [request.user] + _accepted_friends_for(request.user)
    entries = []

    for user in participants:
        metrics = _build_user_metrics(user, today, start_date=window_start)
        entries.append(
            {
                "user": user,
                "is_current_user": user == request.user,
                "total_habits": metrics["total_habits"],
                "overall_completion": metrics["overall_completion"],
                "best_streak": metrics["best_streak"],
                "consistency_score": metrics["consistency_score"],
                "total_scheduled": metrics["total_scheduled"],
                "total_completed": metrics["total_completed"],
            }
        )

    entries.sort(
        key=lambda entry: (
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
            "leaderboard_window_title": window_title,
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
    if raw_date:
        parsed = parse_date(raw_date)
        if parsed:
            return parsed
    return timezone.localdate()


def _extract_authorization_token(authorization_header):
    if not authorization_header:
        return ""

    if authorization_header.startswith("Bearer "):
        return authorization_header.removeprefix("Bearer ").strip()

    return authorization_header.strip()


def _run_cron_job():
    """Placeholder for cron logic that can be extended later."""
    return None
