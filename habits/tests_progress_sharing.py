"""Regression coverage for mutual, yesterday-only Progress Sharing.

The feature deliberately has two boundaries worth testing separately:

* action views own the request/accept/decline/cancel/stop state machine; and
* ``get_shared_yesterday_progress`` is the backend data-access boundary.

These tests freeze the application-local date so schedule and plan-version
assertions do not depend on the day the suite happens to run.
"""

from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import (
    FriendRequest,
    Habit,
    HabitCompletion,
    HabitPause,
    HabitPlanVersion,
    ProgressSharing,
)
from .services import get_shared_yesterday_progress


class ProgressSharingTestMixin:
    TODAY = date(2026, 8, 28)
    YESTERDAY = date(2026, 8, 27)
    OLDER_DAY = date(2026, 8, 20)

    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        # The suite only uses force_login, so unusable passwords keep these
        # state-machine tests quick without changing authentication semantics.
        cls.alice = User.objects.create_user(username="alice", password=None)
        cls.bob = User.objects.create_user(username="bob", password=None)
        cls.cara = User.objects.create_user(username="cara", password=None)

    def make_friendship(self, first=None, second=None):
        return FriendRequest.objects.create(
            from_user=first or self.alice,
            to_user=second or self.bob,
            status=FriendRequest.STATUS_ACCEPTED,
        )

    def make_sharing(
        self,
        friendship,
        requester=None,
        status=ProgressSharing.STATUS_PENDING,
    ):
        participants = sorted(
            (friendship.from_user, friendship.to_user),
            key=lambda user: user.pk,
        )
        return ProgressSharing.objects.create(
            friendship=friendship,
            user_one=participants[0],
            user_two=participants[1],
            requester=requester or friendship.from_user,
            status=status,
            accepted_at=(
                timezone.now() if status == ProgressSharing.STATUS_ACTIVE else None
            ),
        )

    def make_habit(self, user=None, name="Habit", **overrides):
        fields = {
            "user": user or self.bob,
            "name": name,
            "habit_type": Habit.HABIT_BINARY,
            "schedule_type": Habit.SCHEDULE_DAILY,
            "priority": Habit.PRIORITY_MEDIUM,
            "start_date": self.OLDER_DAY,
        }
        fields.update(overrides)
        return Habit.objects.create(**fields)

    def log_progress(self, habit, target_date=None, percentage=100, raw_value=None):
        if raw_value is None:
            raw_value = percentage
        return HabitCompletion.objects.create(
            habit=habit,
            date=target_date or self.YESTERDAY,
            completion_percentage=Decimal(str(percentage)),
            raw_value=Decimal(str(raw_value)),
        )

    def shared_progress(self, viewer=None, profile_user=None):
        with patch(
            "habits.services.timezone.localdate",
            return_value=self.TODAY,
        ):
            return get_shared_yesterday_progress(
                viewer or self.alice,
                profile_user or self.bob,
                today=self.TODAY,
            )

    def get_profile(self, viewer=None, profile_user=None, query=""):
        viewer = viewer or self.alice
        profile_user = profile_user or self.bob
        self.client.force_login(viewer)
        url = reverse("habits:user_profile", args=[profile_user.username])
        if query:
            url = f"{url}?{query}"
        with patch(
            "habits.views.timezone.localdate",
            return_value=self.TODAY,
        ), patch(
            "habits.services.timezone.localdate",
            return_value=self.TODAY,
        ):
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        return response


class ProgressSharingModelTests(ProgressSharingTestMixin, TestCase):
    def test_pair_is_canonical_and_accept_records_one_mutual_relationship(self):
        friendship = self.make_friendship(first=self.bob, second=self.alice)
        sharing = ProgressSharing.objects.create(
            friendship=friendship,
            user_one=self.bob,
            user_two=self.alice,
            requester=self.bob,
        )

        expected_ids = sorted((self.alice.pk, self.bob.pk))
        self.assertEqual(
            [sharing.user_one_id, sharing.user_two_id],
            expected_ids,
        )
        self.assertEqual(ProgressSharing.objects.count(), 1)
        self.assertEqual(sharing.status, ProgressSharing.STATUS_PENDING)

        sharing.accept()
        sharing.refresh_from_db()
        self.assertEqual(sharing.status, ProgressSharing.STATUS_ACTIVE)
        self.assertIsNotNone(sharing.accepted_at)
        self.assertEqual(ProgressSharing.objects.count(), 1)

    def test_database_constraints_reject_invalid_or_duplicate_pairs(self):
        friendship = self.make_friendship()
        one, two = sorted((self.alice, self.bob), key=lambda user: user.pk)

        # bulk_create intentionally bypasses ProgressSharing.save so these
        # assertions prove the database constraints, not just Python checks.
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                ProgressSharing.objects.bulk_create(
                    [
                        ProgressSharing(
                            friendship=friendship,
                            user_one=one,
                            user_two=one,
                            requester=one,
                        )
                    ]
                )

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                ProgressSharing.objects.bulk_create(
                    [
                        ProgressSharing(
                            friendship=friendship,
                            user_one=one,
                            user_two=two,
                            requester=self.cara,
                        )
                    ]
                )

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                ProgressSharing.objects.bulk_create(
                    [
                        ProgressSharing(
                            friendship=friendship,
                            user_one=one,
                            user_two=two,
                            requester=one,
                            status=ProgressSharing.STATUS_ACTIVE,
                            accepted_at=None,
                        )
                    ]
                )

        self.make_sharing(friendship)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                ProgressSharing.objects.create(
                    friendship=friendship,
                    user_one=self.bob,
                    user_two=self.alice,
                    requester=self.bob,
                )

    def test_model_rejects_participants_that_do_not_match_friendship(self):
        friendship = self.make_friendship(first=self.alice, second=self.cara)

        with self.assertRaisesMessage(
            ValueError,
            "Progress Sharing participants must match the friendship.",
        ):
            ProgressSharing.objects.create(
                friendship=friendship,
                user_one=self.alice,
                user_two=self.bob,
                requester=self.alice,
            )

    def test_deleting_friendship_cascades_pending_and_active_sharing(self):
        for status in (
            ProgressSharing.STATUS_PENDING,
            ProgressSharing.STATUS_ACTIVE,
        ):
            with self.subTest(status=status):
                friendship = self.make_friendship()
                sharing = self.make_sharing(friendship, status=status)

                friendship.delete()

                self.assertFalse(
                    ProgressSharing.objects.filter(pk=sharing.pk).exists()
                )


class ProgressSharingActionTests(ProgressSharingTestMixin, TestCase):
    def post_as(self, user, route_name, args, data=None):
        self.client.force_login(user)
        return self.client.post(
            reverse(route_name, args=args),
            data or {},
        )

    def test_only_friends_can_request_and_users_cannot_request_themselves(self):
        nonfriend_response = self.post_as(
            self.alice,
            "habits:request_progress_sharing",
            [self.bob.pk],
        )
        self.assertEqual(nonfriend_response.status_code, 302)
        self.assertFalse(ProgressSharing.objects.exists())

        self_response = self.post_as(
            self.alice,
            "habits:request_progress_sharing",
            [self.alice.pk],
        )
        self.assertEqual(self_response.status_code, 302)
        self.assertFalse(ProgressSharing.objects.exists())

        # A merely pending friendship is not enough.
        FriendRequest.objects.create(from_user=self.alice, to_user=self.bob)
        pending_friend_response = self.post_as(
            self.alice,
            "habits:request_progress_sharing",
            [self.bob.pk],
        )
        self.assertEqual(pending_friend_response.status_code, 302)
        self.assertFalse(ProgressSharing.objects.exists())

    def test_friend_can_send_request_from_either_friendship_direction(self):
        friendship = self.make_friendship(first=self.bob, second=self.alice)

        response = self.post_as(
            self.alice,
            "habits:request_progress_sharing",
            [self.bob.pk],
        )

        self.assertEqual(response.status_code, 302)
        sharing = ProgressSharing.objects.get()
        self.assertEqual(sharing.friendship, friendship)
        self.assertEqual(sharing.requester, self.alice)
        self.assertEqual(sharing.status, ProgressSharing.STATUS_PENDING)
        self.assertEqual(
            [sharing.user_one_id, sharing.user_two_id],
            sorted((self.alice.pk, self.bob.pk)),
        )

    def test_recipient_sees_progress_sharing_notification_with_mutual_access_actions(self):
        friendship = self.make_friendship()
        response = self.post_as(
            self.alice,
            "habits:request_progress_sharing",
            [self.bob.pk],
        )
        self.assertEqual(response.status_code, 302)
        sharing = ProgressSharing.objects.get(friendship=friendship)

        self.client.force_login(self.bob)
        response = self.client.get(reverse("habits:today"))

        self.assertContains(response, "wants to share yesterday's habit progress with you")
        self.assertContains(response, "Accepting gives you both mutual access.")
        self.assertContains(
            response,
            reverse("habits:accept_progress_sharing", args=[sharing.pk]),
        )
        self.assertContains(
            response,
            reverse("habits:decline_progress_sharing", args=[sharing.pk]),
        )

    def test_progress_sharing_notification_is_recipient_only_and_disappears_after_decline(self):
        friendship = self.make_friendship()
        sharing = self.make_sharing(friendship, requester=self.alice)

        self.client.force_login(self.alice)
        requester_response = self.client.get(reverse("habits:today"))
        self.assertNotContains(
            requester_response,
            "wants to share yesterday's habit progress with you",
        )

        self.client.force_login(self.bob)
        recipient_response = self.client.get(reverse("habits:today"))
        self.assertContains(
            recipient_response,
            "wants to share yesterday's habit progress with you",
        )
        decline_response = self.client.post(
            reverse("habits:decline_progress_sharing", args=[sharing.pk]),
            {"next": reverse("habits:today")},
        )
        self.assertEqual(decline_response.status_code, 302)
        self.assertFalse(ProgressSharing.objects.filter(pk=sharing.pk).exists())
        self.assertNotContains(
            self.client.get(reverse("habits:today")),
            "wants to share yesterday's habit progress with you",
        )

    def test_duplicate_and_crossed_requests_converge_on_first_pending_request(self):
        self.make_friendship()
        request_url_args = [self.bob.pk]

        first_response = self.post_as(
            self.alice,
            "habits:request_progress_sharing",
            request_url_args,
        )
        sharing = ProgressSharing.objects.get()
        original_requested_at = sharing.requested_at

        duplicate_response = self.post_as(
            self.alice,
            "habits:request_progress_sharing",
            request_url_args,
        )
        crossed_response = self.post_as(
            self.bob,
            "habits:request_progress_sharing",
            [self.alice.pk],
        )

        self.assertEqual(first_response.status_code, 302)
        self.assertEqual(duplicate_response.status_code, 302)
        self.assertEqual(crossed_response.status_code, 302)
        self.assertEqual(ProgressSharing.objects.count(), 1)
        sharing.refresh_from_db()
        self.assertEqual(sharing.status, ProgressSharing.STATUS_PENDING)
        self.assertEqual(sharing.requester, self.alice)
        self.assertEqual(sharing.requested_at, original_requested_at)

        self.client.force_login(self.bob)
        response = self.client.get(reverse("habits:today"))
        self.assertEqual(
            response.content.decode().count(
                "wants to share yesterday's habit progress with you"
            ),
            1,
        )

    def test_only_recipient_can_accept_a_pending_request(self):
        friendship = self.make_friendship()
        sharing = self.make_sharing(friendship, requester=self.alice)

        requester_attempt = self.post_as(
            self.alice,
            "habits:accept_progress_sharing",
            [sharing.pk],
        )
        outsider_attempt = self.post_as(
            self.cara,
            "habits:accept_progress_sharing",
            [sharing.pk],
        )
        self.assertEqual(requester_attempt.status_code, 404)
        self.assertEqual(outsider_attempt.status_code, 404)
        sharing.refresh_from_db()
        self.assertEqual(sharing.status, ProgressSharing.STATUS_PENDING)

        response = self.post_as(
            self.bob,
            "habits:accept_progress_sharing",
            [sharing.pk],
        )

        self.assertEqual(response.status_code, 302)
        sharing.refresh_from_db()
        self.assertEqual(sharing.status, ProgressSharing.STATUS_ACTIVE)
        self.assertIsNotNone(sharing.accepted_at)

    def test_recipient_can_decline_but_requester_and_outsider_cannot(self):
        friendship = self.make_friendship()
        sharing = self.make_sharing(friendship, requester=self.alice)

        for unauthorized_user in (self.alice, self.cara):
            with self.subTest(user=unauthorized_user.username):
                response = self.post_as(
                    unauthorized_user,
                    "habits:decline_progress_sharing",
                    [sharing.pk],
                )
                self.assertEqual(response.status_code, 404)
                self.assertTrue(
                    ProgressSharing.objects.filter(pk=sharing.pk).exists()
                )

        response = self.post_as(
            self.bob,
            "habits:decline_progress_sharing",
            [sharing.pk],
        )
        self.assertEqual(response.status_code, 302)
        self.assertFalse(ProgressSharing.objects.filter(pk=sharing.pk).exists())

    def test_requester_can_cancel_but_recipient_and_outsider_cannot(self):
        friendship = self.make_friendship()
        sharing = self.make_sharing(friendship, requester=self.alice)

        for unauthorized_user in (self.bob, self.cara):
            with self.subTest(user=unauthorized_user.username):
                response = self.post_as(
                    unauthorized_user,
                    "habits:cancel_progress_sharing",
                    [sharing.pk],
                )
                self.assertEqual(response.status_code, 404)
                self.assertTrue(
                    ProgressSharing.objects.filter(pk=sharing.pk).exists()
                )

        response = self.post_as(
            self.alice,
            "habits:cancel_progress_sharing",
            [sharing.pk],
        )
        self.assertEqual(response.status_code, 302)
        self.assertFalse(ProgressSharing.objects.filter(pk=sharing.pk).exists())

    def test_either_participant_can_revoke_active_sharing(self):
        friendship = self.make_friendship()

        for revoker in (self.alice, self.bob):
            with self.subTest(revoker=revoker.username):
                sharing = self.make_sharing(
                    friendship,
                    status=ProgressSharing.STATUS_ACTIVE,
                )
                response = self.post_as(
                    revoker,
                    "habits:stop_progress_sharing",
                    [sharing.pk],
                )
                self.assertEqual(response.status_code, 302)
                self.assertFalse(
                    ProgressSharing.objects.filter(pk=sharing.pk).exists()
                )

    def test_outsider_cannot_revoke_or_handle_another_pairs_request(self):
        friendship = self.make_friendship()
        pending = self.make_sharing(friendship, requester=self.alice)

        for route_name in (
            "habits:accept_progress_sharing",
            "habits:decline_progress_sharing",
            "habits:cancel_progress_sharing",
        ):
            with self.subTest(route=route_name):
                response = self.post_as(self.cara, route_name, [pending.pk])
                self.assertEqual(response.status_code, 404)
                self.assertTrue(
                    ProgressSharing.objects.filter(pk=pending.pk).exists()
                )

        pending.delete()
        active = self.make_sharing(
            friendship,
            status=ProgressSharing.STATUS_ACTIVE,
        )
        response = self.post_as(
            self.cara,
            "habits:stop_progress_sharing",
            [active.pk],
        )
        self.assertEqual(response.status_code, 404)
        self.assertTrue(ProgressSharing.objects.filter(pk=active.pk).exists())

    def test_unfriending_removes_pending_and_active_sharing(self):
        for status in (
            ProgressSharing.STATUS_PENDING,
            ProgressSharing.STATUS_ACTIVE,
        ):
            with self.subTest(status=status):
                friendship = self.make_friendship()
                sharing = self.make_sharing(friendship, status=status)

                response = self.post_as(
                    self.bob,
                    "habits:remove_friend",
                    [friendship.pk],
                )

                self.assertEqual(response.status_code, 302)
                self.assertFalse(
                    FriendRequest.objects.filter(pk=friendship.pk).exists()
                )
                self.assertFalse(
                    ProgressSharing.objects.filter(pk=sharing.pk).exists()
                )
                if status == ProgressSharing.STATUS_PENDING:
                    self.client.force_login(self.bob)
                    response = self.client.get(reverse("habits:today"))
                    self.assertNotContains(
                        response,
                        "wants to share yesterday's habit progress with you",
                    )

    def test_duplicate_active_and_stale_actions_do_not_change_state(self):
        friendship = self.make_friendship()
        sharing = self.make_sharing(
            friendship,
            status=ProgressSharing.STATUS_ACTIVE,
        )

        duplicate_request = self.post_as(
            self.alice,
            "habits:request_progress_sharing",
            [self.bob.pk],
        )
        stale_accept = self.post_as(
            self.bob,
            "habits:accept_progress_sharing",
            [sharing.pk],
        )
        stale_cancel = self.post_as(
            self.alice,
            "habits:cancel_progress_sharing",
            [sharing.pk],
        )

        self.assertEqual(duplicate_request.status_code, 302)
        self.assertEqual(stale_accept.status_code, 404)
        self.assertEqual(stale_cancel.status_code, 404)
        sharing.refresh_from_db()
        self.assertEqual(sharing.status, ProgressSharing.STATUS_ACTIVE)
        self.assertEqual(ProgressSharing.objects.count(), 1)

        stop_response = self.post_as(
            self.alice,
            "habits:stop_progress_sharing",
            [sharing.pk],
        )
        repeated_stop = self.post_as(
            self.bob,
            "habits:stop_progress_sharing",
            [sharing.pk],
        )
        self.assertEqual(stop_response.status_code, 302)
        self.assertEqual(repeated_stop.status_code, 404)
        self.assertFalse(ProgressSharing.objects.exists())

    def test_mutation_routes_are_login_required_and_post_only(self):
        friendship = self.make_friendship()
        sharing = self.make_sharing(friendship)

        self.client.force_login(self.alice)
        for route_name, args in (
            ("habits:request_progress_sharing", [self.bob.pk]),
            ("habits:accept_progress_sharing", [sharing.pk]),
            ("habits:decline_progress_sharing", [sharing.pk]),
            ("habits:cancel_progress_sharing", [sharing.pk]),
            ("habits:stop_progress_sharing", [sharing.pk]),
        ):
            with self.subTest(route=route_name):
                response = self.client.get(reverse(route_name, args=args))
                self.assertEqual(response.status_code, 405)

        self.client.logout()
        response = self.client.post(
            reverse("habits:cancel_progress_sharing", args=[sharing.pk])
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("habits:login"), response.url)
        self.assertTrue(ProgressSharing.objects.filter(pk=sharing.pk).exists())


class ProgressSharingAccessTests(ProgressSharingTestMixin, TestCase):
    def test_acceptance_gives_mutual_access_to_each_owners_yesterday(self):
        friendship = self.make_friendship()
        sharing = self.make_sharing(friendship, requester=self.alice)
        alice_habit = self.make_habit(user=self.alice, name="Alice yesterday")
        bob_habit = self.make_habit(user=self.bob, name="Bob yesterday")
        self.log_progress(alice_habit)
        self.log_progress(bob_habit)

        self.client.force_login(self.bob)
        response = self.client.post(
            reverse("habits:accept_progress_sharing", args=[sharing.pk])
        )
        self.assertEqual(response.status_code, 302)

        alice_view = self.shared_progress(self.alice, self.bob)
        bob_view = self.shared_progress(self.bob, self.alice)
        self.assertEqual(
            [row["habit_name"] for row in alice_view["rows"]],
            ["Bob yesterday"],
        )
        self.assertEqual(
            [row["habit_name"] for row in bob_view["rows"]],
            ["Alice yesterday"],
        )

    def test_friends_without_active_sharing_cannot_access_progress(self):
        friendship = self.make_friendship()
        private_habit = self.make_habit(name="PRIVATE YESTERDAY HABIT")
        self.log_progress(private_habit)

        self.assertIsNone(self.shared_progress())
        none_response = self.get_profile()
        self.assertIsNone(none_response.context["yesterday_progress"])
        self.assertContains(none_response, "Progress is private.")
        self.assertNotContains(none_response, "PRIVATE YESTERDAY HABIT")

        self.make_sharing(friendship, requester=self.alice)
        self.assertIsNone(self.shared_progress())
        pending_response = self.get_profile()
        self.assertIsNone(pending_response.context["yesterday_progress"])
        self.assertNotContains(pending_response, "PRIVATE YESTERDAY HABIT")

    def test_nonparticipant_cannot_access_by_changing_profile_url_or_ids(self):
        friendship = self.make_friendship()
        self.make_sharing(
            friendship,
            status=ProgressSharing.STATUS_ACTIVE,
        )
        private_habit = self.make_habit(name="BOB SHARED ONLY WITH ALICE")
        self.log_progress(private_habit)

        self.assertIsNone(self.shared_progress(self.cara, self.bob))
        self.assertIsNone(self.shared_progress(self.cara, self.alice))

        changed_profile_response = self.get_profile(self.cara, self.bob)
        self.assertIsNone(changed_profile_response.context["yesterday_progress"])
        self.assertNotContains(
            changed_profile_response,
            "BOB SHARED ONLY WITH ALICE",
        )
        self.assertNotContains(changed_profile_response, "Yesterday's Progress")

    def test_revoking_removes_access_for_both_participants_immediately(self):
        friendship = self.make_friendship()
        sharing = self.make_sharing(
            friendship,
            status=ProgressSharing.STATUS_ACTIVE,
        )
        self.make_habit(user=self.alice, name="Alice private after stop")
        self.make_habit(user=self.bob, name="Bob private after stop")
        self.assertIsNotNone(self.shared_progress(self.alice, self.bob))
        self.assertIsNotNone(self.shared_progress(self.bob, self.alice))

        self.client.force_login(self.alice)
        response = self.client.post(
            reverse("habits:stop_progress_sharing", args=[sharing.pk])
        )
        self.assertEqual(response.status_code, 302)

        self.assertIsNone(self.shared_progress(self.alice, self.bob))
        self.assertIsNone(self.shared_progress(self.bob, self.alice))

    def test_unfriending_or_stale_friendship_status_denies_backend_access(self):
        friendship = self.make_friendship()
        sharing = self.make_sharing(
            friendship,
            status=ProgressSharing.STATUS_ACTIVE,
        )
        self.make_habit(name="No access after friendship ends")
        self.assertIsNotNone(self.shared_progress())

        FriendRequest.objects.filter(pk=friendship.pk).update(
            status=FriendRequest.STATUS_PENDING
        )
        self.assertIsNone(self.shared_progress())

        friendship.delete()
        self.assertFalse(ProgressSharing.objects.filter(pk=sharing.pk).exists())
        self.assertIsNone(self.shared_progress())

    def test_arbitrary_date_query_never_exposes_older_or_future_progress(self):
        friendship = self.make_friendship()
        self.make_sharing(
            friendship,
            status=ProgressSharing.STATUS_ACTIVE,
        )
        yesterday_habit = self.make_habit(name="YESTERDAY ALLOWED")
        self.log_progress(yesterday_habit)

        older_habit = self.make_habit(
            name="OLDER HISTORY MUST STAY PRIVATE",
            schedule_type=Habit.SCHEDULE_INTERVAL,
            interval_days=999,
            start_date=self.OLDER_DAY,
        )
        self.log_progress(older_habit, target_date=self.OLDER_DAY)
        future_habit = self.make_habit(
            name="FUTURE MUST STAY PRIVATE",
            start_date=self.TODAY + timedelta(days=1),
        )

        response = self.get_profile(
            query=(
                f"date={self.OLDER_DAY.isoformat()}&"
                f"habit_id={older_habit.pk}&user_id={future_habit.pk}"
            )
        )

        self.assertEqual(response.context["yesterday_progress"]["date"], self.YESTERDAY)
        self.assertContains(response, "YESTERDAY ALLOWED")
        self.assertNotContains(response, "OLDER HISTORY MUST STAY PRIVATE")
        self.assertNotContains(response, "FUTURE MUST STAY PRIVATE")


class YesterdayProgressDataTests(ProgressSharingTestMixin, TestCase):
    def setUp(self):
        self.friendship = self.make_friendship()
        self.sharing = self.make_sharing(
            self.friendship,
            status=ProgressSharing.STATUS_ACTIVE,
        )

    def test_only_habits_scheduled_yesterday_are_returned(self):
        included = self.make_habit(name="Scheduled yesterday")

        older_only = self.make_habit(
            name="Older only",
            schedule_type=Habit.SCHEDULE_INTERVAL,
            interval_days=999,
            start_date=self.OLDER_DAY,
        )
        # Even malformed/legacy completion data must not make an unscheduled
        # habit visible.
        self.log_progress(older_only, percentage=100)

        self.make_habit(
            name="Starts in future",
            start_date=self.TODAY + timedelta(days=1),
        )
        paused = self.make_habit(name="Paused yesterday")
        HabitPause.objects.create(
            habit=paused,
            start_date=self.YESTERDAY,
            end_date=self.TODAY,
        )

        payload = self.shared_progress()
        names = [row["habit_name"] for row in payload["rows"]]

        self.assertEqual(names, [included.name])
        self.assertEqual(payload["scheduled_count"], 1)

    def test_quantitative_progress_includes_fractional_amount_target_and_unit(self):
        quantitative = self.make_habit(
            name="Drink water",
            habit_type=Habit.HABIT_QUANTITATIVE,
            target_value=Decimal("8"),
            unit="glasses",
        )
        self.log_progress(
            quantitative,
            percentage=Decimal("81.25"),
            raw_value=Decimal("6.5"),
        )

        payload = self.shared_progress()
        row = payload["rows"][0]

        self.assertEqual(row["habit_name"], "Drink water")
        self.assertEqual(row["habit_type"], Habit.HABIT_QUANTITATIVE)
        self.assertEqual(row["completed_amount"], 6.5)
        self.assertEqual(row["target_amount"], Decimal("8"))
        self.assertEqual(row["unit"], "glasses")
        response = self.get_profile()
        self.assertContains(response, "6.50 / 8 glasses")

    def test_binary_habits_show_completed_and_not_completed_states(self):
        completed = self.make_habit(name="Exercise")
        missed = self.make_habit(name="Meditation")
        self.log_progress(completed, percentage=100)
        self.log_progress(missed, percentage=0)

        rows = {
            row["habit_name"]: row
            for row in self.shared_progress()["rows"]
        }
        self.assertTrue(rows["Exercise"]["completed"])
        self.assertFalse(rows["Meditation"]["completed"])

        response = self.get_profile()
        content = response.content.decode()
        exercise_row = content.split("Exercise", 1)[1].split("</li>", 1)[0]
        meditation_row = content.split("Meditation", 1)[1].split("</li>", 1)[0]
        self.assertIn("Completed", exercise_row)
        self.assertIn("Not completed", meditation_row)

    def test_summary_counts_fully_completed_habits_not_weighted_partial_progress(self):
        completed = self.make_habit(
            name="Low priority completed",
            priority=Habit.PRIORITY_LOW,
        )
        missed = self.make_habit(
            name="High priority missed",
            priority=Habit.PRIORITY_HIGH,
        )
        partial = self.make_habit(
            name="Partially measured",
            habit_type=Habit.HABIT_QUANTITATIVE,
            target_value=Decimal("10"),
            unit="pages",
        )
        self.log_progress(completed, percentage=100)
        self.log_progress(missed, percentage=0)
        self.log_progress(partial, percentage=50, raw_value=5)

        payload = self.shared_progress()

        self.assertEqual(payload["scheduled_count"], 3)
        self.assertEqual(payload["completed_count"], 1)
        self.assertEqual(payload["completion_rate"], 33)

    def test_yesterday_uses_historical_target_unit_and_type_after_today_edit(self):
        habit = self.make_habit(
            name="Historical water",
            habit_type=Habit.HABIT_QUANTITATIVE,
            target_value=Decimal("10"),
            unit="bottles",
        )
        HabitPlanVersion.objects.create(
            habit=habit,
            effective_from=self.OLDER_DAY,
            schedule_anchor=self.OLDER_DAY,
            habit_type=Habit.HABIT_QUANTITATIVE,
            target_value=Decimal("8"),
            unit="glasses",
            schedule_type=Habit.SCHEDULE_DAILY,
            interval_days=1,
            weekly_interval=1,
            priority=Habit.PRIORITY_MEDIUM,
        )
        HabitPlanVersion.objects.create(
            habit=habit,
            effective_from=self.TODAY,
            schedule_anchor=self.OLDER_DAY,
            habit_type=Habit.HABIT_QUANTITATIVE,
            target_value=Decimal("10"),
            unit="bottles",
            schedule_type=Habit.SCHEDULE_DAILY,
            interval_days=1,
            weekly_interval=1,
            priority=Habit.PRIORITY_MEDIUM,
        )
        self.log_progress(habit, percentage=75, raw_value=6)

        row = self.shared_progress()["rows"][0]

        self.assertEqual(row["habit_type"], Habit.HABIT_QUANTITATIVE)
        self.assertEqual(row["completed_amount"], 6)
        self.assertEqual(row["target_amount"], Decimal("8"))
        self.assertEqual(row["unit"], "glasses")
        response = self.get_profile()
        self.assertContains(response, "6 / 8 glasses")
        self.assertNotContains(response, "6 / 10 bottles")

    def test_yesterday_schedule_is_resolved_from_effective_plan(self):
        historically_scheduled = self.make_habit(
            name="Historical daily habit",
            schedule_type=Habit.SCHEDULE_INTERVAL,
            interval_days=999,
            start_date=self.TODAY,
        )
        HabitPlanVersion.objects.create(
            habit=historically_scheduled,
            effective_from=self.OLDER_DAY,
            schedule_anchor=self.OLDER_DAY,
            habit_type=Habit.HABIT_BINARY,
            schedule_type=Habit.SCHEDULE_DAILY,
            interval_days=1,
            weekly_interval=1,
            priority=Habit.PRIORITY_MEDIUM,
        )
        HabitPlanVersion.objects.create(
            habit=historically_scheduled,
            effective_from=self.TODAY,
            schedule_anchor=self.TODAY,
            habit_type=Habit.HABIT_BINARY,
            schedule_type=Habit.SCHEDULE_INTERVAL,
            interval_days=999,
            weekly_interval=1,
            priority=Habit.PRIORITY_MEDIUM,
        )

        historically_unscheduled = self.make_habit(
            name="Historical off-day habit",
            schedule_type=Habit.SCHEDULE_DAILY,
        )
        HabitPlanVersion.objects.create(
            habit=historically_unscheduled,
            effective_from=self.OLDER_DAY,
            schedule_anchor=self.YESTERDAY - timedelta(days=1),
            habit_type=Habit.HABIT_BINARY,
            schedule_type=Habit.SCHEDULE_INTERVAL,
            interval_days=2,
            weekly_interval=1,
            priority=Habit.PRIORITY_MEDIUM,
        )
        HabitPlanVersion.objects.create(
            habit=historically_unscheduled,
            effective_from=self.TODAY,
            schedule_anchor=self.TODAY,
            habit_type=Habit.HABIT_BINARY,
            schedule_type=Habit.SCHEDULE_DAILY,
            interval_days=1,
            weekly_interval=1,
            priority=Habit.PRIORITY_MEDIUM,
        )

        names = [
            row["habit_name"]
            for row in self.shared_progress()["rows"]
        ]
        self.assertIn("Historical daily habit", names)
        self.assertNotIn("Historical off-day habit", names)

    def test_payload_is_narrow_and_does_not_leak_descriptions_tags_or_history(self):
        habit = self.make_habit(
            name="Allowed habit name",
            description="SECRET DESCRIPTION MUST NOT LEAK",
            tags="SECRET-TAG-MUST-NOT-LEAK",
        )
        self.log_progress(habit)
        older = self.make_habit(
            name="SECRET OLDER HABIT NAME",
            description="SECRET OLD DESCRIPTION",
            schedule_type=Habit.SCHEDULE_INTERVAL,
            interval_days=999,
            start_date=self.OLDER_DAY,
        )
        self.log_progress(older, target_date=self.OLDER_DAY)

        payload = self.shared_progress()
        self.assertEqual(
            set(payload["rows"][0]),
            {
                "habit_name",
                "habit_type",
                "completed",
                "completion_percentage",
                "completed_amount",
                "target_amount",
                "unit",
            },
        )

        response = self.get_profile()
        self.assertContains(response, "Allowed habit name")
        for secret in (
            "SECRET DESCRIPTION MUST NOT LEAK",
            "SECRET-TAG-MUST-NOT-LEAK",
            "SECRET OLDER HABIT NAME",
            "SECRET OLD DESCRIPTION",
        ):
            with self.subTest(secret=secret):
                self.assertNotContains(response, secret)

    def test_no_scheduled_habits_has_distinct_empty_state(self):
        response = self.get_profile()

        payload = response.context["yesterday_progress"]
        self.assertEqual(payload["scheduled_count"], 0)
        self.assertEqual(payload["completed_count"], 0)
        self.assertEqual(payload["completion_rate"], 0)
        self.assertEqual(payload["rows"], [])
        self.assertContains(response, "No habits were scheduled yesterday.")
        self.assertNotContains(response, "Progress is private.")

    def test_partial_habit_has_safe_percentage_rendering(self):
        habit = self.make_habit(
            name="Partial cleanup",
            habit_type=Habit.HABIT_PARTIAL,
        )
        self.log_progress(habit, percentage=Decimal("42.5"))

        response = self.get_profile()
        row = response.context["yesterday_progress"]["rows"][0]
        self.assertEqual(row["completion_percentage"], 42.5)
        self.assertContains(response, "42.5% complete")


class ProgressSharingProfileStateTests(ProgressSharingTestMixin, TestCase):
    def test_profile_renders_none_outgoing_incoming_and_active_states(self):
        friendship = self.make_friendship()

        none_response = self.get_profile(self.alice, self.bob)
        self.assertContains(none_response, "Progress is private.")
        self.assertContains(none_response, "Request Progress Sharing")
        self.assertContains(
            none_response,
            "If your friend accepts, both of you will be able to see each other's yesterday progress.",
        )

        sharing = self.make_sharing(friendship, requester=self.alice)
        outgoing_response = self.get_profile(self.alice, self.bob)
        self.assertContains(outgoing_response, "Progress sharing request sent.")
        self.assertContains(outgoing_response, "Cancel Request")

        incoming_response = self.get_profile(self.bob, self.alice)
        self.assertContains(
            incoming_response,
            "alice requested Progress Sharing.",
        )
        self.assertContains(incoming_response, ">Accept<")
        self.assertContains(incoming_response, ">Decline<")

        sharing.accept()
        active_response = self.get_profile(self.alice, self.bob)
        self.assertContains(active_response, "Stop Progress Sharing")
        self.assertContains(active_response, "data-confirm-action")
        self.assertContains(
            active_response,
            "Stopping Progress Sharing will remove access for both of you.",
        )

    def test_tooltips_use_accessible_existing_help_pattern(self):
        self.make_friendship()

        response = self.get_profile()

        self.assertContains(response, 'class="score-driver-help"', count=2)
        self.assertContains(response, "data-score-driver-help", count=2)
        self.assertContains(response, 'role="tooltip"', count=2)
        self.assertContains(
            response,
            f'aria-describedby="progress-sharing-help-{self.bob.pk}" '
            'aria-expanded="false" data-score-driver-help',
        )
        self.assertContains(
            response,
            f'aria-describedby="yesterday-progress-help-{self.bob.pk}" '
            'aria-expanded="false" data-score-driver-help',
        )
        self.assertContains(
            response,
            "Progress Sharing lets you and a friend mutually see each other's scheduled habit progress from yesterday.",
        )
        self.assertContains(
            response,
            "Shows the habits this user had scheduled yesterday and how much of each habit they completed.",
        )

    def test_nonfriend_and_own_profile_do_not_offer_progress_sharing(self):
        nonfriend_response = self.get_profile(self.alice, self.bob)
        self.assertNotContains(nonfriend_response, "Yesterday's Progress")
        self.assertNotContains(nonfriend_response, "Request Progress Sharing")

        own_response = self.get_profile(self.alice, self.alice)
        self.assertNotContains(own_response, "Yesterday's Progress")
        self.assertNotContains(own_response, "Request Progress Sharing")
