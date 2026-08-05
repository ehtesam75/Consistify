from django.utils import timezone

from .models import FriendRequest
from .services import (
    daily_recap_target_date,
    get_pending_habits_for_date,
    should_prompt_daily_recap,
)


def friend_request_notifications(request):
    if not request.user.is_authenticated:
        return {
            "incoming_friend_request_count": 0,
            "incoming_friend_requests": [],
        }

    incoming_requests = (
        FriendRequest.objects.filter(
            to_user=request.user,
            status=FriendRequest.STATUS_PENDING,
        )
        .select_related("from_user")
        .order_by("-created_at")
    )

    return {
        "incoming_friend_request_count": incoming_requests.count(),
        "incoming_friend_requests": incoming_requests[:5],
    }


def daily_recap_prompt(request):
    if not request.user.is_authenticated:
        return {"daily_recap": None}

    today = timezone.localdate()
    target_date = daily_recap_target_date(today)
    expected_recap_value = target_date.isoformat()
    recap_date_value = request.session.get("daily_recap_date")
    if recap_date_value and recap_date_value != expected_recap_value:
        request.session.pop("daily_recap_date", None)
        recap_date_value = None

    if not recap_date_value:
        if not should_prompt_daily_recap(request.user.last_login, today):
            return {"daily_recap": None}

        dismissed_for = request.session.get("daily_recap_dismissed_for")
        if dismissed_for == expected_recap_value:
            return {"daily_recap": None}

        request.session["daily_recap_date"] = expected_recap_value
        recap_date_value = request.session["daily_recap_date"]

    pending = get_pending_habits_for_date(request.user, target_date)
    if not pending:
        request.session.pop("daily_recap_date", None)
        request.session["daily_recap_dismissed_for"] = target_date.isoformat()
        return {"daily_recap": None}

    return {
        "daily_recap": {
            "date": target_date,
            "pending_habits": pending,
        }
    }
