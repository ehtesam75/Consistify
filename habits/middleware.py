"""Project middleware.

The custom exception middleware is the single source of structured logging for
unhandled errors raised anywhere in the request/response cycle. Every entry it
emits contains the request method, full path, user identifier (or
``anonymous``), remote address, user agent, and the full Python traceback so
the production log aggregator can correlate the failure with a specific
endpoint, user, or deployment.

When the failing request is an AJAX call (``X-Requested-With: XMLHttpRequest``
or an ``Accept`` header that prefers JSON) the middleware short-circuits
Django's HTML 500 page and returns a safe JSON envelope. Front-end code can
therefore always assume a parseable response, even when the server itself
hits an unexpected exception.
"""

from __future__ import annotations

import json
import logging
import traceback
from typing import Any

from django.http import JsonResponse

logger = logging.getLogger("habits.errors")


class RequestIdLogFormatter(logging.Formatter):
    """Format helper that always supplies a ``request_id``.

    The verbose formatter references ``%(request_id)s`` so a single line can be
    tied to the originating request. Django's built-in log records (such as
    those emitted from ``log_response`` and ``process_exception`` paths) do not
    carry that attribute by default, which would otherwise crash the logging
    pipeline with ``KeyError``. This formatter substitutes ``"-"`` so those
    records still render cleanly.
    """

    def format(self, record):
        if not hasattr(record, "request_id"):
            record.request_id = "-"
        return super().format(record)


def _client_ip(request) -> str:
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR")
    if forwarded:
        # X-Forwarded-For can be a comma-separated chain; the originating
        # client is the first entry.
        return forwarded.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR", "")


def _safe_request_payload(request) -> dict[str, Any]:
    """Return a serialisable snapshot of the request for log aggregation.

    Sensitive headers (Authorization, Cookie) are scrubbed so structured logs
    can be shipped to a third-party log aggregator without leaking secrets.
    """

    sensitive = {"HTTP_AUTHORIZATION", "HTTP_COOKIE"}
    headers = {
        key[len("HTTP_"):].replace("_", "-").title(): value
        for key, value in request.META.items()
        if key.startswith("HTTP_") and key not in sensitive
    }
    return {
        "method": request.method,
        "path": request.path,
        "full_path": request.get_full_path(),
        "scheme": request.scheme,
        "is_secure": request.is_secure(),
        "host": request.get_host(),
        "remote_ip": _client_ip(request),
        "user_agent": request.META.get("HTTP_USER_AGENT", ""),
        "referer": request.META.get("HTTP_REFERER", ""),
        "ajax": request.headers.get("X-Requested-With") == "XMLHttpRequest",
        "accept": request.headers.get("Accept", ""),
        "content_type": request.headers.get("Content-Type", ""),
        "user_id": (
            request.user.pk
            if getattr(request, "user", None) and request.user.is_authenticated
            else None
        ),
        "username": (
            request.user.get_username()
            if getattr(request, "user", None) and request.user.is_authenticated
            else ""
        ),
        "headers": headers,
    }


def _wants_json_response(request) -> bool:
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return True
    accept = request.headers.get("Accept", "")
    return "application/json" in accept and "text/html" not in accept


class StructuredExceptionMiddleware:
    """Log unhandled exceptions with full request context.

    The middleware sits at the outermost layer so it sees every request,
    including 404s, 403s, 500s, and any exception raised inside Django's own
    middleware (sessions, auth, CSRF, security, etc.).

    The middleware deliberately lets exceptions propagate so Django's normal
    ``handler500`` (our ``habits.error_handlers.server_error``) renders the
    HTML error page for normal browsers. The ``process_exception`` hook
    provides the structured log entry and returns a JSON 500 envelope when
    the caller is an AJAX request — short-circuiting Django's HTML handler
    so the front-end can recover gracefully.

    Note: ``process_exception`` is part of Django's new-style middleware API
    and is invoked by ``BaseHandler.get_response`` for every middleware that
    defines it. Returning a ``HttpResponse`` from it prevents Django from
    converting the exception to the default 500 page.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        return self.get_response(request)

    def process_exception(self, request, exception):
        request_payload = _safe_request_payload(request)
        logger.error(
            "Unhandled server error on %s %s",
            request.method,
            request.get_full_path(),
            exc_info=exception,
            extra={"request": request_payload},
        )

        if _wants_json_response(request):
            return JsonResponse(
                {
                    "ok": False,
                    "error": "An unexpected server error occurred.",
                    "code": "internal_server_error",
                },
                status=500,
            )

        # Returning ``None`` lets Django continue through ``handler500`` so
        # ``habits.error_handlers.server_error`` renders the HTML page.
        return None


class RequestContextLogMiddleware:
    """Attach a request id to every log record made during the request.

    Renderers downstream can use ``record.request_id`` to stitch together log
    lines for a single user action, even when many logger calls fire from
    different modules.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        import secrets

        request_id = request.headers.get("X-Request-Id") or secrets.token_hex(8)
        request.request_id = request_id

        old_factory = logging.getLogRecordFactory()

        def _factory(*args, **kwargs):
            record = old_factory(*args, **kwargs)
            record.request_id = getattr(request, "request_id", "")
            return record

        logging.setLogRecordFactory(_factory)
        try:
            response = self.get_response(request)
        finally:
            logging.setLogRecordFactory(old_factory)

        response["X-Request-Id"] = request_id
        return response
