from django.contrib.auth import views as auth_views
from django.urls import path

from . import views

app_name = "habits"

urlpatterns = [
    path("", views.index, name="index"),
    path("cron-job/", views.cron_job, name="cron_job"),
    path("today/", views.habit_list, name="today"),
    path("dashboard/", views.dashboard, name="dashboard"),
    path("reports/", views.reports, name="reports"),
    path("compare/", views.habit_compare, name="habit_compare"),
    path("profile/", views.profile, name="profile"),
    path("habits/new/", views.habit_create, name="habit_create"),
    path("habits/<int:habit_id>/", views.habit_detail, name="habit_detail"),
    path("habits/<int:habit_id>/edit/", views.habit_edit, name="habit_edit"),
    path("habits/<int:habit_id>/toggle/", views.update_progress, name="toggle_completion"),
    path("habits/<int:habit_id>/progress/", views.update_progress, name="update_progress"),
    path("habits/reorder/", views.reorder_habits, name="reorder_habits"),
    path("accounts/signup/", views.signup, name="signup"),
    path("accounts/login/", views.ConsistifyLoginView.as_view(), name="login"),
    path("accounts/logout/", auth_views.LogoutView.as_view(), name="logout"),
]
