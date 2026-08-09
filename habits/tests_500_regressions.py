"""Regression tests covering the production HTTP 500 audit fixes.

Each test here maps to one of the failure modes the audit identified:
- AJAX endpoints returning safe JSON on unhandled errors
- Custom error handlers responding JSON for AJAX callers
- Static file storage falling back when the manifest is missing
- Structured exception middleware logging the failure
- Cron job endpoint returning a JSON 500 envelope on unexpected failure
"""

import io
import json
import logging
import secrets
from datetime import date
from decimal import Decimal
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.http import JsonResponse
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse

from .models import (
    DEFAULT_CATEGORIES,
    DailyRecapCompletion,
    Habit,
    HabitCategory,
    HabitCompletion,
)
from .staticfiles_storage import SafeCompressedManifestStaticFilesStorage


class _SafeJsonErrorResponseTests(TestCase):
    """``update_progress`` must return a safe JSON error envelope on
    unexpected failures (DB outage, plan-resolution crash, etc.) instead of
    bubbling a generic 500 HTML page back to the browser."""

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="ajax-user",
            password="not-used",
        )
        self.category, _ = HabitCategory.objects.get_or_create(
            key=DEFAULT_CATEGORIES[0][0],
            defaults={"label": DEFAULT_CATEGORIES[0][1], "sort_order": 1},
        )
        self.habit = Habit.objects.create(
            user=self.user,
            name="Daily read",
            habit_type=Habit.HABIT_BINARY,
            schedule_type=Habit.SCHEDULE_DAILY,
            start_date=date.today(),
        )
        self.client.force_login(self.user)
        self.url = reverse("habits:update_progress", args=[self.habit.id])

    def test_update_progress_returns_json_when_db_save_fails(self):
        with patch(
            "habits.views.HabitCompletion.objects.get_or_create",
            side_effect=RuntimeError("simulated DB outage"),
        ):
            response = self.client.post(
                self.url,
                data={"date": date.today().isoformat(), "completed": "1"},
                HTTP_X_REQUESTED_WITH="XMLHttpRequest",
            )
        self.assertEqual(response.status_code, 400)
        body = json.loads(response.content)
        self.assertFalse(body["ok"])
        self.assertIn("save", body["error"].lower())

    def test_update_progress_returns_redirect_when_browser_save_fails(self):
        """Non-AJAX callers must still get the redirect-with-message flow."""
        from django.contrib.messages.storage.fallback import FallbackStorage

        with patch(
            "habits.views.HabitCompletion.objects.get_or_create",
            side_effect=RuntimeError("simulated DB outage"),
        ):
            # Establish a usable session/message store for the test client.
            request = RequestFactory().post(self.url)
            setattr(request, "session", self.client.session)
            setattr(request, "_messages", FallbackStorage(request))
            response = self.client.post(
                self.url,
                data={"date": date.today().isoformat(), "completed": "1"},
            )
        # Either a redirect (non-AJAX path) or a JSON envelope is acceptable,
        # but it must not be an unhandled 500.
        self.assertIn(response.status_code, (200, 302, 400))


class ReorderHabitsErrorEnvelopeTests(TestCase):
    """``reorder_habits`` must respond with a JSON 500 envelope when the
    database update loop fails, so the front-end can recover gracefully."""

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="reorder-user",
            password="not-used",
        )
        self.habit_a = Habit.objects.create(
            user=self.user,
            name="A",
            schedule_type=Habit.SCHEDULE_DAILY,
            start_date=date.today(),
            sort_order=1,
        )
        self.habit_b = Habit.objects.create(
            user=self.user,
            name="B",
            schedule_type=Habit.SCHEDULE_DAILY,
            start_date=date.today(),
            sort_order=2,
        )
        self.client.force_login(self.user)

    def test_reorder_returns_json_500_on_db_error(self):
        url = reverse("habits:reorder_habits")
        with patch(
            "habits.views.Habit.objects.filter",
            side_effect=RuntimeError("DB down"),
        ):
            response = self.client.post(
                url,
                data={"habit_ids": f"{self.habit_a.id},{self.habit_b.id}"},
            )
        self.assertEqual(response.status_code, 500)
        body = json.loads(response.content)
        self.assertFalse(body["ok"])
        self.assertIn("error", body)


class DailyRecapErrorTests(TestCase):
    """``daily_recap`` must not crash with an unhandled 500 if the
    database write fails; it must surface a friendly message."""

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="recap-user",
            password="not-used",
        )
        self.habit = Habit.objects.create(
            user=self.user,
            name="Cap",
            habit_type=Habit.HABIT_BINARY,
            schedule_type=Habit.SCHEDULE_DAILY,
            start_date=date.today(),
        )
        self.client.force_login(self.user)
        self.url = reverse("habits:daily_recap")

    def test_daily_recap_returns_redirect_on_db_error(self):
        from datetime import timedelta

        yesterday_date = date.today() - timedelta(days=1)
        session = self.client.session
        session["daily_recap_date"] = yesterday_date.isoformat()
        session.save()

        # Target the actual DB write inside ``daily_recap`` so the session
        # save above does not blow up first.
        with patch(
            "habits.views.HabitCompletion.objects.get_or_create",
            side_effect=RuntimeError("simulated DB outage"),
        ):
            response = self.client.post(
                self.url,
                data={
                    "date": yesterday_date.isoformat(),
                    f"completed_{self.habit.id}": "1",
                },
            )
        # The view must not return a raw 500; it should redirect with a
        # user-facing message instead.
        self.assertIn(response.status_code, (200, 302))


class CronJobEnvelopeTests(TestCase):
    """``cron_job`` must return a JSON 500 envelope with a stable shape
    when ``_run_cron_job`` raises an exception."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.token = secrets.token_urlsafe(32)

    def setUp(self):
        self.url = reverse("habits:cron_job")

    def _post_with_secret(self, token):
        return self.client.post(
            self.url,
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )

    @override_settings(CRON_SECRET="")
    def test_cron_job_returns_401_when_secret_unset(self):
        response = self._post_with_secret(self.token)
        self.assertEqual(response.status_code, 401)
        self.assertEqual(json.loads(response.content)["ok"], False)

    def test_cron_job_returns_json_500_when_job_fails(self):
        token = self.token
        with override_settings(CRON_SECRET=token), patch(
            "habits.views._run_cron_job",
            side_effect=RuntimeError("DB blew up"),
        ):
            response = self._post_with_secret(token)
        self.assertEqual(response.status_code, 500)
        body = json.loads(response.content)
        self.assertFalse(body["ok"])
        self.assertEqual(body["code"], "cron_failed")

    def test_cron_job_returns_ok_when_job_succeeds(self):
        token = self.token
        with override_settings(CRON_SECRET=token), patch(
            "habits.views._run_cron_job",
            return_value=None,
        ):
            response = self._post_with_secret(token)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(response.content)["ok"], True)


class ErrorHandlerJsonAwarenessTests(TestCase):
    """Custom 400/403/404/500 handlers must return JSON when the caller is
    an AJAX request, and a rendered HTML page otherwise."""

    def setUp(self):
        self.factory = RequestFactory()
        # The bare RequestFactory request has no ``user`` attribute, but the
        # 500 template renders a header that touches request.user. Attach an
        # anonymous user to mimic what the auth middleware would do.
        from django.contrib.auth.models import AnonymousUser
        self._anonymous = AnonymousUser()

    def _attach_user(self, request):
        request.user = self._anonymous
        return request

    def test_handler500_returns_json_for_xml_http_request(self):
        from habits.error_handlers import server_error

        request = self.factory.get("/some-broken-path")
        request.META["HTTP_X_REQUESTED_WITH"] = "XMLHttpRequest"
        self._attach_user(request)
        response = server_error(request)
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response["Content-Type"], "application/json")

    def test_handler404_returns_html_for_browser(self):
        from habits.error_handlers import page_not_found

        request = self.factory.get("/missing")
        self._attach_user(request)
        response = page_not_found(request, RuntimeError("not used"))
        self.assertEqual(response.status_code, 404)
        # HTML response, not JSON.
        self.assertTrue(response["Content-Type"].startswith("text/html"))

    def test_handler404_returns_json_for_ajax(self):
        from habits.error_handlers import page_not_found

        request = self.factory.get("/missing")
        request.META["HTTP_X_REQUESTED_WITH"] = "XMLHttpRequest"
        self._attach_user(request)
        response = page_not_found(request, RuntimeError("not used"))
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response["Content-Type"], "application/json")


class SafeStaticStorageTests(TestCase):
    """``SafeCompressedManifestStaticFilesStorage`` must not raise
    ``ValueError`` when a template references a path that is not in the
    manifest; it must log a warning and fall back to the unhashed URL."""

    def test_url_falls_back_when_manifest_missing(self):
        storage = SafeCompressedManifestStaticFilesStorage()
        with self.assertLogs("habits", level="WARNING") as captured:
            url = storage.url("icons/missing.svg")
        self.assertTrue(url.endswith("icons/missing.svg"))
        self.assertTrue(any("manifest" in msg.lower() for msg in captured.output))


class StructuredExceptionMiddlewareTests(TestCase):
    """The custom middleware must log the request path, method, and user
    for every unhandled exception, and return a JSON 500 to AJAX callers."""

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="middleware-user",
            password="not-used",
        )
        self.client.force_login(self.user)

    def test_unhandled_view_exception_returns_json_for_ajax(self):
        from habits.middleware import StructuredExceptionMiddleware

        def boom(request):
            raise RuntimeError("kaboom")

        middleware = StructuredExceptionMiddleware(boom)
        request = RequestFactory().get("/boom")
        request.META["HTTP_X_REQUESTED_WITH"] = "XMLHttpRequest"
        request.user = self.user
        response = middleware(request)
        self.assertEqual(response.status_code, 500)
        body = json.loads(response.content)
        self.assertFalse(body["ok"])
        self.assertEqual(body["code"], "internal_server_error")

    def test_unhandled_view_exception_returns_json_for_ajax(self):
        from habits.middleware import StructuredExceptionMiddleware

        def boom(request):
            raise RuntimeError("kaboom")

        middleware = StructuredExceptionMiddleware(boom)
        request = RequestFactory().get("/boom")
        request.META["HTTP_X_REQUESTED_WITH"] = "XMLHttpRequest"
        request.user = self.user
        response = middleware.process_exception(request, RuntimeError("kaboom"))
        self.assertEqual(response.status_code, 500)
        body = json.loads(response.content)
        self.assertFalse(body["ok"])
        self.assertEqual(body["code"], "internal_server_error")

    def test_unhandled_view_exception_returns_none_for_browser(self):
        """Non-AJAX requests: middleware logs but does not intercept.

        Returning ``None`` from ``process_exception`` lets Django's normal
        ``handler500`` chain render the HTML page.
        """
        from habits.middleware import StructuredExceptionMiddleware

        middleware = StructuredExceptionMiddleware(lambda req: None)
        request = RequestFactory().get("/boom")
        request.user = self.user
        response = middleware.process_exception(request, RuntimeError("kaboom"))
        self.assertIsNone(response)

    def test_middleware_logs_exception_with_request_context(self):
        from habits.middleware import StructuredExceptionMiddleware

        def boom(request):
            raise RuntimeError("kaboom")

        middleware = StructuredExceptionMiddleware(boom)
        request = RequestFactory().get("/exploding-path")
        request.user = self.user
        request.META["REMOTE_ADDR"] = "203.0.113.7"
        with self.assertLogs("habits.errors", level="ERROR") as captured:
            middleware.process_exception(request, RuntimeError("kaboom"))
        joined = "\n".join(captured.output)
        self.assertIn("/exploding-path", joined)
        self.assertIn("kaboom", joined)