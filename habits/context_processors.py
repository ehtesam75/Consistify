from datetime import timedelta

from django.utils import timezone
from django.utils.dateparse import parse_date

from .models import FriendRequest
from .services import get_pending_habits_for_date, should_prompt_daily_recap


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
    recap_date_value = request.session.get("daily_recap_date")
    if not recap_date_value:
        if not should_prompt_daily_recap(request.user.last_login, today):
            return {"daily_recap": None}

        dismissed_for = request.session.get("daily_recap_dismissed_for")
        target_date = today - timedelta(days=1)
        if dismissed_for == target_date.isoformat():
            return {"daily_recap": None}

        request.session["daily_recap_date"] = target_date.isoformat()
        recap_date_value = request.session["daily_recap_date"]

    target_date = parse_date(recap_date_value)
    if not target_date:
        request.session.pop("daily_recap_date", None)
        return {"daily_recap": None}

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
