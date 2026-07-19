"""RFC-7807 (`application/problem+json`) error shaping (T0.6).

Wired as `REST_FRAMEWORK["EXCEPTION_HANDLER"]` (see `config/settings/base.py`)
so every DRF-raised error (auth failures, throttling, permission denials,
validation errors, ...) across the whole API — not just the T0.6 auth
endpoints — comes back in the same problem+json envelope, per
`docs/api-and-ui.md` §1 ("Errors: RFC-7807-style problem+json.") and
`CLAUDE.md`.

Shape:
    {
        "type": "about:blank" | "https://.../problems/<slug>",
        "title": "<short machine-ish description>",
        "status": <http status code>,
        "detail": "<human-readable detail>",
        "errors": {...}        # only present for field-validation errors
        "retry_after": "<n>"   # only present when throttled
    }
"""

from __future__ import annotations

from typing import Any, Optional

from django.http import JsonResponse
from rest_framework.response import Response
from rest_framework.views import exception_handler as _drf_exception_handler

PROBLEM_CONTENT_TYPE = "application/problem+json"


def problem_response(
    *,
    status_code: int,
    title: str,
    detail: str = "",
    type_: str = "about:blank",
    **extra: Any,
) -> Response:
    """Build an RFC-7807 response directly (for hand-written view code, e.g.
    the T0.6 login view's uniform "invalid credentials" / "locked" errors,
    which are not raised as DRF exceptions)."""
    body: dict[str, Any] = {"type": type_, "title": title, "status": status_code}
    if detail:
        body["detail"] = detail
    body.update(extra)
    response = Response(body, status=status_code)
    response["Content-Type"] = PROBLEM_CONTENT_TYPE
    return response


def rfc7807_exception_handler(exc, context) -> Optional[Response]:
    """DRF `EXCEPTION_HANDLER` — reshapes the default DRF error response into
    RFC-7807 problem+json. Returns `None` (DRF's default: an unhandled 500)
    when the default handler doesn't recognize the exception, same as DRF's
    own `exception_handler`.
    """
    response = _drf_exception_handler(exc, context)
    if response is None:
        return None

    title = exc.__class__.__name__
    detail_data = response.data
    errors: Any = None

    if isinstance(detail_data, dict) and set(detail_data.keys()) <= {"detail"}:
        detail = str(detail_data.get("detail", ""))
    elif isinstance(detail_data, dict):
        detail = "Validation failed."
        errors = detail_data
    elif isinstance(detail_data, list):
        detail = "; ".join(str(d) for d in detail_data)
    else:
        detail = str(detail_data)

    body: dict[str, Any] = {
        "type": "about:blank",
        "title": title,
        "status": response.status_code,
        "detail": detail,
    }
    if errors is not None:
        body["errors"] = errors

    retry_after = response.get("Retry-After")
    if retry_after is not None:
        body["retry_after"] = retry_after

    response.data = body
    response["Content-Type"] = PROBLEM_CONTENT_TYPE
    return response


def csrf_failure_view(request, reason: str = "") -> JsonResponse:
    """`CSRF_FAILURE_VIEW` (see `config/settings/base.py`).

    A CSRF rejection happens in `django.middleware.csrf.CsrfViewMiddleware`
    (invoked directly by the `@csrf_protect` decorator on `LoginView`, since
    DRF's `APIView.as_view()` marks views `csrf_exempt` by default — see
    `apps.accounts.api` module docstring) — i.e. *before* DRF's own
    `EXCEPTION_HANDLER` ever runs, so without this it would fall back to
    Django's default HTML 403 page. This keeps every error on the API,
    including this one, in the same RFC-7807 problem+json envelope.
    """
    body = {
        "type": "about:blank",
        "title": "CSRF verification failed",
        "status": 403,
        "detail": reason or "CSRF verification failed. Request aborted.",
    }
    return JsonResponse(body, status=403, content_type=PROBLEM_CONTENT_TYPE)
