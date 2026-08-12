"""Authenticated media serving (`GET /media/<storage_key>`).

Until now nginx served `/media/` straight off the volume with no auth check at
all: anyone holding the exact URL could open the file. The paths carry a random
UUID so they cannot be guessed, but a link that escapes — pasted into a chat,
forwarded by email, left in browser history on a shared machine — kept working
forever for whoever held it. That is an acceptable trade for an asset photo and
a poor one for a **bank statement**, which is what the M8 expense work started
storing.

## How it works

The request now reaches Django, which authorizes it and then hands the actual
file-serving back to nginx via **`X-Accel-Redirect`**. Django never reads or
streams the bytes, so a 40 MB PDF does not occupy a gunicorn worker for the
length of the download — nginx does what it is good at, and Django only makes
the decision. The internal location the header points at is marked `internal`
in the nginx config, so it cannot be requested directly from outside.

## What is checked

1. **Authenticated.** No session, no file.
2. **Same tenant.** Every storage key this codebase writes is laid out as
   `<prefix>/<tenant_id>/<anchor_id>/<uuid>_<filename>`
   (`apps.assets.services.attachment_upload_path`), so the tenant that owns the
   file is in the path itself. A user of tenant B is refused tenant A's file
   even with a valid session and the exact URL — the R4 rule, applied to bytes
   rather than rows.

**Deliberate limit, worth being explicit about:** this is *not* per-object RBAC.
Any authenticated member of the owning tenant can fetch any of that tenant's
files if they have the link — a Member could open a project's invoice scan
that the project hub itself would not show them. Closing that gap means
resolving each key back to its owning row and re-running the matching
permission check, which needs a key->model registry this codebase does not
have. The jump from "the whole internet" to "one authenticated colleague in
your own lab" is the large part of the risk, and is what this view delivers;
the remainder is recorded here rather than silently implied.

In DEBUG (`config.settings.dev`) there is no nginx in front, so the view falls
back to streaming the file itself — otherwise `runserver` would return an
`X-Accel-Redirect` header no one acts on, and every attachment would appear
broken in local development.
"""

from __future__ import annotations

import posixpath
import re

from django.conf import settings
from django.core.files.storage import default_storage
from django.http import FileResponse, Http404, HttpResponse
from django.views import View

#: The `internal` nginx location that actually serves the bytes. Must match
#: `docker/nginx/default.conf`.
INTERNAL_MEDIA_PREFIX = "/protected-media/"

#: `<prefix>/<tenant_id>/<rest>` — the layout every writer in this codebase
#: uses. Keys that do not match are refused rather than guessed at.
_TENANT_SCOPED_KEY = re.compile(r"^[A-Za-z0-9._-]+/(?P<tenant_id>\d+)/.+$")


def _is_safe_key(key: str) -> bool:
    """Reject traversal and absolute paths before touching the filesystem.

    `posixpath.normpath` collapses `..`; if the result differs from the input
    the caller was trying to climb out of the media root.
    """
    if not key or key.startswith("/") or "\\" in key:
        return False
    return posixpath.normpath(key) == key


class ProtectedMediaView(View):
    """Serve an uploaded file to an authenticated user of the owning tenant."""

    def get(self, request, key: str):
        user = getattr(request, "user", None)
        if user is None or not user.is_authenticated:
            # 404, not 403: a stranger holding a leaked link learns nothing
            # about whether it names a real file.
            raise Http404

        if not _is_safe_key(key):
            raise Http404

        match = _TENANT_SCOPED_KEY.match(key)
        if match is None or int(match.group("tenant_id")) != user.tenant_id:
            raise Http404

        if not default_storage.exists(key):
            raise Http404

        if settings.DEBUG:
            # No nginx in front of `runserver` — stream it directly so local
            # development isn't a wall of broken links.
            return FileResponse(default_storage.open(key, "rb"))

        response = HttpResponse(status=200)
        response["X-Accel-Redirect"] = INTERNAL_MEDIA_PREFIX + key
        # Let nginx set the length/type from the file on disk.
        del response["Content-Type"]
        return response
