"""Static-file storage backend used in production.

The default WhiteNoise ``CompressedManifestStaticFilesStorage`` raises
``ValueError`` (which Django turns into a 500) whenever a template references
a static file that did not make it into the manifest during ``collectstatic``.
That can happen with optional assets such as PWA icons that are referenced
from a manifest, a service worker, or a conditional template.

This subclass logs a warning and falls back to the unhashed URL so the page
still renders. The next ``collectstatic`` run will pick the missing file up
automatically once it is committed, so no production data is lost.
"""

from __future__ import annotations

import logging

from django.contrib.staticfiles.storage import StaticFilesStorage
from whitenoise.storage import CompressedManifestStaticFilesStorage

logger = logging.getLogger("habits.staticfiles")


class SafeCompressedManifestStaticFilesStorage(
    CompressedManifestStaticFilesStorage
):
    """``CompressedManifestStaticFilesStorage`` that tolerates missing files.

    On a missing-manifest lookup we emit a WARNING log so the production log
    aggregator can surface the gap, then delegate to ``StaticFilesStorage``
    (the plain, unhashed lookup) so the page still renders the file. The
    next ``collectstatic`` run will pick the missing file up automatically
    once it is committed.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Plain, unhashed lookup used as the fallback. Cached so we don't
        # re-instantiate it on every fallback.
        self._fallback_storage = StaticFilesStorage()

    def url(self, name, *args, **kwargs):  # type: ignore[override]
        try:
            return super().url(name, *args, **kwargs)
        except ValueError as exc:
            logger.warning(
                "Static file %r is missing from the manifest; "
                "falling back to the unhashed URL. Cause: %s",
                name,
                exc,
            )
            return self._fallback_storage.url(name, *args, **kwargs)
