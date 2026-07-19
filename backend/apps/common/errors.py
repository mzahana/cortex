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

from django.db import IntegrityError, transaction
from django.db.models.deletion import ProtectedError
from django.http import JsonResponse
from rest_framework import status
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


def _mark_atomic_rollback_and_respond(
    *, status_code: int, title: str, detail: str, type_: str
) -> Response:
    """Build the RFC-7807 response for a DB-layer exception we're
    deliberately swallowing here (`ProtectedError`/`IntegrityError` below)
    instead of letting it propagate.

    Mirrors what DRF's own default `exception_handler` does for
    `APIException` (see its `set_rollback()` call): under
    `ATOMIC_REQUESTS=True` (`config/settings/base.py`), the whole
    request/view call is wrapped in one `transaction.atomic()` block by
    Django's own handler. That block only rolls back if either (a) the
    exception propagates all the way out of it, or (b) something inside it
    explicitly calls `transaction.set_rollback(True)`. We're doing neither by
    default here — we catch the exception and return a normal `Response` —
    so without this call, Django would try to `COMMIT` an already-aborted
    Postgres transaction (a `ProtectedError`/`IntegrityError` aborts the
    transaction at the DB level immediately) once the view returns
    "successfully" with our 4xx body, raising a second, unrelated error.
    """
    transaction.set_rollback(True)
    return problem_response(status_code=status_code, title=title, detail=detail, type_=type_)


def _protected_error_response(exc: ProtectedError) -> Response:
    """`django.db.models.deletion.ProtectedError` -> `409 Conflict`.

    Raised whenever a `models.PROTECT` FK blocks a delete (e.g. deleting a
    `Category`/`Location` that still has children — T1.1's tree models; this
    is deliberately generic so any future `on_delete=PROTECT` relationship,
    including T1.2's `Asset` FKs, gets the same RFC-7807 4xx instead of an
    unhandled 500).
    """
    protected_objects = getattr(exc, "protected_objects", None)
    count: Optional[int] = None
    if protected_objects is not None:
        try:
            count = len(protected_objects)
        except TypeError:
            count = None
    detail = "Cannot delete this record: other records still reference it."
    if count:
        detail += f" ({count} referencing record{'s' if count != 1 else ''})."
    return _mark_atomic_rollback_and_respond(
        status_code=status.HTTP_409_CONFLICT,
        title="Conflict",
        detail=detail,
        type_="about:blank",
    )


def _integrity_error_response(exc: IntegrityError) -> Response:
    """`django.db.IntegrityError` -> `409 Conflict` (defense-in-depth fallback).

    Serializer-level validation (see `apps.catalog.serializers`) is the
    preferred, clean-400-with-field-error path for expected uniqueness
    conflicts. This is the safety net for whatever slips past it anyway
    (e.g. a race between two concurrent requests both passing validation
    before either commits) — general on purpose so it covers every future
    tenant-owned model's `UniqueConstraint`, not just T1.1's, without an
    unhandled 500 leaking a DB error message to the client.
    """
    return _mark_atomic_rollback_and_respond(
        status_code=status.HTTP_409_CONFLICT,
        title="Conflict",
        detail="This request conflicts with an existing record (e.g. a duplicate value).",
        type_="about:blank",
    )


def rfc7807_exception_handler(exc, context) -> Optional[Response]:
    """DRF `EXCEPTION_HANDLER` — reshapes the default DRF error response into
    RFC-7807 problem+json. Returns `None` (DRF's default: an unhandled 500)
    when the default handler doesn't recognize the exception, same as DRF's
    own `exception_handler`.

    `ProtectedError`/`IntegrityError` are handled BEFORE delegating to DRF's
    own `exception_handler`: neither is a DRF `APIException`/`Http404`/
    `PermissionDenied`, so the default handler would return `None` for them,
    which propagates to Django's unhandled-exception path (an HTML 500) —
    breaking the "every error is RFC-7807 problem+json" convention
    (`docs/api-and-ui.md` §1, `CLAUDE.md`) for exactly the DB-constraint
    cases every tenant-owned model with a `UniqueConstraint`/`PROTECT` FK
    can hit.
    """
    if isinstance(exc, ProtectedError):
        return _protected_error_response(exc)
    if isinstance(exc, IntegrityError):
        return _integrity_error_response(exc)

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
