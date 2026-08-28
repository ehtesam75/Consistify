from django.urls import path

from . import views

app_name = "habits"

urlpatterns = [
    path("", views.index, name="index"),
    path("health/", views.health, name="health"),
    path("cron-job/", views.cron_job, name="cron_job"),
    path("today/", views.habit_list, name="today"),
    path("today/all-habits/", views.mobile_all_habits, name="mobile_all_habits"),
    path("archived/", views.archived_habits, name="archived_habits"),

    path("dashboard/", views.dashboard, name="dashboard"),
    path("reports/", views.reports, name="reports"),
    path("compare/", views.habit_compare, name="habit_compare"),
    path("leaderboard/", views.leaderboard, name="leaderboard"),
    path("profile/", views.profile, name="profile"),
    path("friends/search/", views.user_search, name="user_search"),
    path("friends/request/<int:user_id>/", views.send_friend_request, name="send_friend_request"),
    path(
        "friends/requests/<int:request_id>/accept/",
        views.accept_friend_request,
        name="accept_friend_request",
    ),
    path(
        "friends/requests/<int:request_id>/cancel/",
        views.cancel_friend_request,
        name="cancel_friend_request",
    ),
    path(
        "friends/requests/<int:request_id>/remove/",
        views.remove_friend,
        name="remove_friend",
    ),
    path(
        "friends/progress-sharing/request/<int:user_id>/",
        views.request_progress_sharing,
        name="request_progress_sharing",
    ),
    path(
        "friends/progress-sharing/<int:sharing_id>/accept/",
        views.accept_progress_sharing,
        name="accept_progress_sharing",
    ),
    path(
        "friends/progress-sharing/<int:sharing_id>/decline/",
        views.decline_progress_sharing,
        name="decline_progress_sharing",
    ),
    path(
        "friends/progress-sharing/<int:sharing_id>/cancel/",
        views.cancel_progress_sharing,
        name="cancel_progress_sharing",
    ),
    path(
        "friends/progress-sharing/<int:sharing_id>/stop/",
        views.stop_progress_sharing,
        name="stop_progress_sharing",
    ),
    path("daily-recap/", views.daily_recap, name="daily_recap"),
    path("habits/new/", views.habit_create, name="habit_create"),
    path("habits/<int:habit_id>/", views.habit_detail, name="habit_detail"),
    path("habits/<int:habit_id>/edit/", views.habit_edit, name="habit_edit"),
    path("habits/<int:habit_id>/pause/", views.pause_habit, name="pause_habit"),
    path("habits/pause-all/", views.pause_all_habits, name="pause_all_habits"),
    path("habits/resume-all/", views.resume_all_habits, name="resume_all_habits"),
    path("habits/<int:habit_id>/resume/", views.resume_habit, name="resume_habit"),
    path("habits/<int:habit_id>/archive/", views.archive_habit, name="archive_habit"),
    path(
        "habits/<int:habit_id>/unarchive/",
        views.unarchive_habit,
        name="unarchive_habit",
    ),
    path("habits/<int:habit_id>/delete/", views.habit_delete, name="habit_delete"),

    path("habits/<int:habit_id>/toggle/", views.update_progress, name="toggle_completion"),
    path("habits/<int:habit_id>/progress/", views.update_progress, name="update_progress"),
    path("habits/reorder/", views.reorder_habits, name="reorder_habits"),
    path("accounts/signup/", views.signup, name="signup"),
    path("accounts/login/", views.ConsistifyLoginView.as_view(), name="login"),
    path("accounts/logout/", views.ConsistifyLogoutView.as_view(), name="logout"),
    path("<str:username>/", views.username_profile, name="user_profile"),
]
