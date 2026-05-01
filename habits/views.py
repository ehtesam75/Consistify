from datetime import timedelta
import json

from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import UserCreationForm
from django.contrib.auth import views as auth_views
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.views.decorators.http import require_POST

from .forms import HabitForm
from .models import Habit, HabitCompletion
from .services import (
    calculate_streaks,
    completion_stats,
    get_completion_map,
    get_next_scheduled_date,
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

    habits = list(Habit.objects.filter(user=request.user).order_by("name"))
    completions = HabitCompletion.objects.filter(habit__in=habits, date=target_date)
    completion_map = {completion.habit_id: completion.completed for completion in completions}

    scheduled_habits = []
    for habit in habits:
        if habit.is_scheduled_on(target_date):
            scheduled_habits.append(
                {
                    "habit": habit,
                    "completed": completion_map.get(habit.id, False),
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

    habits = list(Habit.objects.filter(user=request.user).order_by("name"))
    completions = HabitCompletion.objects.filter(
        habit__in=habits,
        date__range=(window_start, today),
        completed=True,
    )

    completion_by_habit = {}
    completion_by_date = {}
    for completion in completions:
        completion_by_habit.setdefault(completion.habit_id, set()).add(completion.date)
        completion_by_date[completion.date] = completion_by_date.get(completion.date, 0) + 1

    habit_cards = []
    total_scheduled = 0
    total_completed = 0

    for habit in habits:
        scheduled_dates = list(iter_scheduled_dates(habit, window_start, today))
        scheduled_total = len(scheduled_dates)
        completed_dates = completion_by_habit.get(habit.id, set())
        completed_total = sum(1 for date in scheduled_dates if date in completed_dates)

        total_scheduled += scheduled_total
        total_completed += completed_total

        completion_rate = (
            round((completed_total / scheduled_total) * 100, 1) if scheduled_total else 0.0
        )
        habit_cards.append(
            {
                "habit": habit,
                "scheduled": scheduled_total,
                "completed": completed_total,
                "rate": completion_rate,
            }
        )

    overall_rate = (
        round((total_completed / total_scheduled) * 100, 1) if total_scheduled else 0.0
    )

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
    window_completion_map = {
        date: True for date in completion_map if date >= window_start
    }
    stats = completion_stats(habit, window_start, today, window_completion_map)
    current_streak, max_streak = calculate_streaks(habit, today)

    today_completion = HabitCompletion.objects.filter(habit=habit, date=today).first()
    today_completed = today_completion.completed if today_completion else False
    is_scheduled_today = habit.is_scheduled_on(today)

    next_due = get_next_scheduled_date(habit, today)

    chart_labels = json.dumps([date.strftime("%b %d") for date in recent_dates])
    chart_completed = json.dumps(
        [1 if completion_map.get(date) else 0 for date in recent_dates]
    )

    context = {
        "habit": habit,
        "history": history,
        "stats": stats,
        "current_streak": current_streak,
        "max_streak": max_streak,
        "today": today,
        "today_completed": today_completed,
        "is_scheduled_today": is_scheduled_today,
        "next_due": next_due,
        "chart_labels": chart_labels,
        "chart_completed": chart_completed,
    }
    return render(request, "habits/habit_detail.html", context)


@login_required
def habit_create(request):
    if request.method == "POST":
        form = HabitForm(request.POST)
        if form.is_valid():
            habit = form.save(commit=False)
            habit.user = request.user
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
