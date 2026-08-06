from django.utils import timezone

from .models import FriendRequest
from .services import should_show_daily_recap


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
    """Decide the recap prompt from the user's persisted database state.

    Visibility is resolved on every request from the stored recap record and
    stored completions, never from session, browser, or device state. Finishing
    the recap anywhere therefore hides it everywhere, on any device, browser,
    session, or repeated login.

    ``daily_recap_date`` is still written to the session, but only as a CSRF-style
    guard so the recap POST can confirm the form was server-issued for the
    current target date. It never decides whether the prompt is shown.
    """
    if not request.user.is_authenticated:
        return {"daily_recap": None}

    today = timezone.localdate()
    should_show, target_date, pending = should_show_daily_recap(request.user, today)

    if not should_show:
        request.session.pop("daily_recap_date", None)
        return {"daily_recap": None}

    request.session["daily_recap_date"] = target_date.isoformat()

    return {
        "daily_recap": {
            "date": target_date,
            "pending_habits": pending,
        }
    }
