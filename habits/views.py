import json
from datetime import timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth import views as auth_views
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import UserCreationForm
from django.db.models import Max
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.views.decorators.http import require_POST

from .forms import HabitForm
from .models import Habit, HabitCompletion
from .services import (
    build_monthly_reports,
    build_weekly_reports,
    calculate_overall_consistency,
    calculate_streaks,
    compare_habits,
    completion_stats,
    get_completion_maps,
    get_next_scheduled_date,
    habit_performance_metrics,
    iter_scheduled_dates,
)


def index(request):
    if request.user.is_authenticated:
        return redirect("habits:today")
    return redirect("habits:login")


class ConsistifyLoginView(auth_views.LoginView):
    template_name = "registration/login.html"


@login_required
def habit_list(request):
    target_date = _get_date_from_request(request)
    habits = list(Habit.objects.filter(user=request.user).order_by("sort_order", "name"))
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

    context = {
        "target_date": target_date,
        "prev_date": target_date - timedelta(days=1),
        "next_date": target_date + timedelta(days=1),
        "scheduled_habits": scheduled_habits,
        "scheduled_count": len(scheduled_habits),
        "completed_count": completed_count,
        "habits": habits,
        "all_count": len(habits),
    }
    return render(request, "habits/habit_list.html", context)


@login_required
def dashboard(request):
    today = timezone.localdate()
    window_start = today - timedelta(days=29)
    habits = list(Habit.objects.filter(user=request.user).order_by("sort_order", "name"))

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
            }
        )

    overall_rate = round(total_completion / total_scheduled, 1) if total_scheduled else 0.0
    overall_consistency = calculate_overall_consistency(habits, window_start, today)

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
        rate = round(sum(daily_values) / len(daily_values), 1) if daily_values else 0.0
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
        "doing_well": doing_well,
        "needs_focus": needs_focus,
        "chart_labels": json.dumps(chart_labels),
        "chart_rates": json.dumps(chart_rates),
    }
    return render(request, "habits/dashboard.html", context)


@login_required
def habit_detail(request, habit_id):
    habit = get_object_or_404(Habit, id=habit_id, user=request.user)
    today = timezone.localdate()
    history_start = today - timedelta(days=120)
    history_dates = list(iter_scheduled_dates(habit, history_start, today))
    recent_dates = history_dates[-30:]

    completion_map, value_map = get_completion_maps(habit, history_start, today)
    history = [
        {
            "date": scheduled_date,
            "completion_percentage": completion_map.get(scheduled_date, 0),
            "completed": completion_map.get(scheduled_date, 0) >= 100,
        }
        for scheduled_date in recent_dates
    ]

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
    detailed_metrics = habit_performance_metrics(
        habit,
        window_start,
        today,
        window_completion_map,
        window_value_map,
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

    chart_labels = json.dumps([date.strftime("%b %d") for date in recent_dates])
    chart_percentages = json.dumps([completion_map.get(date, 0) for date in recent_dates])

    context = {
        "habit": habit,
        "history": history,
        "stats": stats,
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
    habit = get_object_or_404(Habit, id=habit_id, user=request.user)
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
        },
    )


@login_required
@require_POST
def update_progress(request, habit_id):
    habit = get_object_or_404(Habit, id=habit_id, user=request.user)
    target_date = _get_date_from_request(request)

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
    habits = list(Habit.objects.filter(user=request.user).order_by("sort_order", "name"))

    weekly_reports = build_weekly_reports(habits, weeks=8, today=today)
    monthly_reports = build_monthly_reports(habits, months=6, today=today)

    weekly_labels = [item["label"] for item in weekly_reports]
    weekly_rates = [item["completion_rate"] for item in weekly_reports]
    weekly_streak = [item["avg_current_streak"] for item in weekly_reports]

    monthly_labels = [item["label"] for item in monthly_reports]
    monthly_rates = [item["completion_rate"] for item in monthly_reports]
    monthly_consistency = [item["consistency_score"] for item in monthly_reports]

    context = {
        "today": today,
        "weekly_reports": weekly_reports,
        "monthly_reports": monthly_reports,
        "weekly_labels": json.dumps(weekly_labels),
        "weekly_rates": json.dumps(weekly_rates),
        "weekly_streak": json.dumps(weekly_streak),
        "monthly_labels": json.dumps(monthly_labels),
        "monthly_rates": json.dumps(monthly_rates),
        "monthly_consistency": json.dumps(monthly_consistency),
    }
    return render(request, "habits/reports.html", context)


@login_required
def habit_compare(request):
    habits = list(Habit.objects.filter(user=request.user).order_by("sort_order", "name"))
    today = timezone.localdate()
    window_start = today - timedelta(days=89)

    habit_a = None
    habit_b = None
    comparison = None

    habit_a_id = request.GET.get("habit_a")
    habit_b_id = request.GET.get("habit_b")

    if habit_a_id and habit_b_id and habit_a_id != habit_b_id:
        try:
            habit_a = Habit.objects.get(id=int(habit_a_id), user=request.user)
            habit_b = Habit.objects.get(id=int(habit_b_id), user=request.user)
            comparison = compare_habits(habit_a, habit_b, window_start, today)
        except (ValueError, Habit.DoesNotExist):
            comparison = None

    context = {
        "habits": habits,
        "habit_a": habit_a,
        "habit_b": habit_b,
        "comparison": comparison,
        "window_start": window_start,
        "today": today,
    }
    return render(request, "habits/habit_compare.html", context)


@login_required
def profile(request):
    today = timezone.localdate()
    habits = list(Habit.objects.filter(user=request.user).order_by("sort_order", "name"))

    if habits:
        start_date = min(habit.start_date for habit in habits)
    else:
        start_date = today

    total_habits = len(habits)
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

    monthly_reports = build_monthly_reports(habits, months=12, today=today)
    progress_labels = [item["label"] for item in monthly_reports]
    progress_rates = [item["completion_rate"] for item in monthly_reports]

    context = {
        "today": today,
        "total_habits": total_habits,
        "overall_completion": overall_completion,
        "best_streak": best_streak,
        "consistency_score": consistency_score,
        "total_scheduled": total_scheduled,
        "total_completed": total_completed,
        "monthly_reports": monthly_reports,
        "progress_labels": json.dumps(progress_labels),
        "progress_rates": json.dumps(progress_rates),
    }
    return render(request, "habits/profile.html", context)


def signup(request):
    if request.method == "POST":
        form = UserCreationForm(request.POST)
        if form.is_valid():
            user = form.save()
            login(request, user)
            messages.success(request, "Welcome to Consistify.")
            return redirect("habits:today")
    else:
        form = UserCreationForm()

    return render(request, "registration/signup.html", {"form": form})


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
