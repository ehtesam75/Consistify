import json
from datetime import timedelta

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
    get_completion_map,
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
    completion_map = {completion.habit_id: completion.completed for completion in completions}

    scheduled_habits = []
    for habit in habits:
        if habit.is_scheduled_on(target_date):
            scheduled_habits.append(
                {
                    "habit": habit,
                    "completed": completion_map.get(habit.id, False),
                    "tags": habit.get_tags(),
                }
            )

    completed_count = sum(1 for item in scheduled_habits if item["completed"])

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
    completions = HabitCompletion.objects.filter(
        habit__in=habits,
        date__range=(window_start, today),
        completed=True,
    )
    completion_by_date = {}
    for completion in completions:
        completion_by_date[completion.date] = completion_by_date.get(completion.date, 0) + 1

    habit_cards = []
    total_scheduled = 0
    total_completed = 0

    for habit in habits:
        metrics = habit_performance_metrics(habit, window_start, today)
        total_scheduled += metrics["scheduled_total"]
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

    overall_rate = (
        round((total_completed / total_scheduled) * 100, 1) if total_scheduled else 0.0
    )
    overall_consistency = calculate_overall_consistency(habits, window_start, today)

    doing_well = [card for card in habit_cards if card["scheduled"] and card["rate"] >= 80]
    needs_focus = [card for card in habit_cards if card["scheduled"] and card["rate"] < 50]

    chart_days = 14
    chart_start = today - timedelta(days=chart_days - 1)
    chart_labels = []
    chart_rates = []

    for offset in range(chart_days):
        current_day = chart_start + timedelta(days=offset)
        scheduled_count = sum(1 for habit in habits if habit.is_scheduled_on(current_day))
        completed_count = completion_by_date.get(current_day, 0)
        rate = (
            round((completed_count / scheduled_count) * 100, 1) if scheduled_count else 0.0
        )
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

    completion_map = get_completion_map(habit, history_start, today)
    history = [
        {
            "date": scheduled_date,
            "completed": completion_map.get(scheduled_date, False),
        }
        for scheduled_date in recent_dates
    ]

    window_start = today - timedelta(days=29)
    window_completion_map = {date: True for date in completion_map if date >= window_start}
    stats = completion_stats(habit, window_start, today, window_completion_map)
    detailed_metrics = habit_performance_metrics(habit, window_start, today, window_completion_map)
    current_streak, max_streak = calculate_streaks(habit, today)

    today_completion = HabitCompletion.objects.filter(habit=habit, date=today).first()
    today_completed = today_completion.completed if today_completion else False
    is_scheduled_today = habit.is_scheduled_on(today)
    next_due = get_next_scheduled_date(habit, today)

    chart_labels = json.dumps([date.strftime("%b %d") for date in recent_dates])
    chart_completed = json.dumps([1 if completion_map.get(date) else 0 for date in recent_dates])

    context = {
        "habit": habit,
        "history": history,
        "stats": stats,
        "metrics": detailed_metrics,
        "current_streak": current_streak,
        "max_streak": max_streak,
        "today": today,
        "today_completed": today_completed,
        "is_scheduled_today": is_scheduled_today,
        "next_due": next_due,
        "chart_labels": chart_labels,
        "chart_completed": chart_completed,
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
def toggle_completion(request, habit_id):
    habit = get_object_or_404(Habit, id=habit_id, user=request.user)
    target_date = _get_date_from_request(request)

    if not habit.is_scheduled_on(target_date):
        messages.error(request, "This habit is not scheduled for that day.")
        return redirect("habits:today")

    completion, created = HabitCompletion.objects.get_or_create(
        habit=habit,
        date=target_date,
        defaults={"completed": True},
    )

    if not created:
        completion.completed = not completion.completed
        completion.save(update_fields=["completed"])

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
    total_completed = 0
    best_streak = 0

    for habit in habits:
        metrics = habit_performance_metrics(habit, start_date, today)
        total_scheduled += metrics["scheduled_total"]
        total_completed += metrics["completed_total"]
        if metrics["max_streak"] > best_streak:
            best_streak = metrics["max_streak"]

    overall_completion = (
        round((total_completed / total_scheduled) * 100, 1) if total_scheduled else 0.0
    )
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


def _get_date_from_request(request):
    raw_date = request.GET.get("date") or request.POST.get("date")
    if raw_date:
        parsed = parse_date(raw_date)
        if parsed:
            return parsed
    return timezone.localdate()
