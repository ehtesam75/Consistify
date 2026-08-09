"""Custom error handlers.

Django looks up these callables in ``ROOT_URLCONF`` when an exception bubbles
out of the request/response cycle. We return JSON envelopes when the client
signals that it is an AJAX caller, and render friendly HTML otherwise. The
same shape is used for 404, 403, 400, and 500 so front-end code can rely on a
consistent ``{ok, error, code}`` body for every failure response.
"""

from __future__ import annotations

import logging
import traceback

from django.http import JsonResponse
from django.shortcuts import render
from django.template import TemplateDoesNotExist
from django.views.decorators.csrf import requires_csrf_token
from django.views.decorators.http import require_GET

logger = logging.getLogger("habits.errors")


def _wants_json(request) -> bool:
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return True
    accept = request.headers.get("Accept", "")
    return "application/json" in accept and "text/html" not in accept


def _json_error(code: str, message: str, status: int) -> JsonResponse:
    return JsonResponse(
        {"ok": False, "error": message, "code": code},
        status=status,
    )


@require_GET
@requires_csrf_token
def page_not_found(request, exception=None):
    if _wants_json(request):
        return _json_error(
            "not_found",
            "The page you requested was not found.",
            404,
        )
    try:
        response = render(request, "habits/404.html", status=404)
    except TemplateDoesNotExist:
        response = render(
            request,
            "registration/login.html",
            status=404,
        )
    return response


@require_GET
def server_error(request):
    # ``logger.exception`` would not work here because there is no active
    # exception. Log a warning so 500s are not silently swallowed, but keep
    # the log level modest because the upstream middleware already records
    # the full traceback.
    logger.warning(
        "Reached handler500 for %s %s",
        request.method,
        request.get_full_path(),
    )
    if _wants_json(request):
        return _json_error(
            "internal_server_error",
            "An unexpected server error occurred.",
            500,
        )
    try:
        response = render(request, "habits/500.html", status=500)
    except TemplateDoesNotExist:
        response = render(
            request,
            "registration/login.html",
            status=500,
        )
    return response


def bad_request(request, exception=None):
    if _wants_json(request):
        return _json_error(
            "bad_request",
            "The request could not be processed.",
            400,
        )
    return render(request, "habits/400.html", status=400)


def permission_denied(request, exception=None):
    if _wants_json(request):
        return _json_error(
            "permission_denied",
            "You do not have permission to perform this action.",
            403,
        )
    return render(request, "habits/403.html", status=403)