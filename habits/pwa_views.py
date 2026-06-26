from django.http import HttpResponse
from django.template.loader import render_to_string
from django.views.decorators.cache import cache_control
from django.views.decorators.http import require_GET


@require_GET
@cache_control(max_age=0, no_cache=True, no_store=True, must_revalidate=True)
def service_worker(request):
    content = render_to_string("pwa/service-worker.js", request=request)
    response = HttpResponse(content, content_type="application/javascript; charset=utf-8")
    response["Service-Worker-Allowed"] = "/"
    return response


@require_GET
@cache_control(max_age=86400)
def manifest(request):
    content = render_to_string("pwa/manifest.json", request=request)
    return HttpResponse(content, content_type="application/manifest+json; charset=utf-8")
