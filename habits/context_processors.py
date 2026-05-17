from .models import FriendRequest


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
