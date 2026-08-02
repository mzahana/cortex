"""Service-layer helpers for user-account management.

Django 5/DRF app-wide gap fill: before this module there was NO way to
create a new `User` anywhere in the app except Django admin or a one-off
`manage.py shell` session — `apps.rbac.api.MembershipViewSet` only lets an
Admin/ProjectLead grant a ROLE to an EXISTING user id (`MembershipSerializer.
user` is a `PrimaryKeyRelatedField`), so account creation itself had no
endpoint at all. `create_user_with_generated_password` is the ONE place that
does it, called from `apps.accounts.api.UserViewSet.create`.

Security notes (do not relax these without re-reading `docs/rbac.md` §5 and
CLAUDE.md "Audit everything mutating" / "no secrets in the audit log"):

- The initial password is generated with `secrets` (a CSPRNG), never
  `random` — matches the "admin creates the account, hands credentials to
  the user out of band" flow rather than a client-chosen-password flow
  (which would open the door to weak, client-supplied passwords).
- It is hashed via `User.set_password` (Argon2, same path
  `apps.accounts.managers.UserManager._create_user` already uses)
  IMMEDIATELY, before the row is ever saved — the plaintext value is never
  persisted anywhere.
- It is returned to the caller (the view) exactly once, as the return
  value's `initial_password` attribute, so the view can put it in the HTTP
  response body one time. It must never be passed to
  `apps.audit.services.write_audit_log`, logged, or stored — see
  `apps.accounts.api.UserViewSet.create`, which builds the audit "after"
  snapshot from `_user_snapshot()` (id/email/name only) rather than from
  this return value directly, so there is no path by which the password
  could accidentally leak into the audit trail.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings
from django.contrib.auth.password_validation import validate_password
from django.utils import timezone

from apps.tenancy.context import TenantContextError, get_current_tenant_id

from .models import PasswordResetToken, User

# Audit `action` strings (free-form per `apps.audit.models.AuditLog.action`,
# documented in `docs/rbac.md` §5). Kept here so the views and any test refer
# to one constant rather than re-typing the literal.
USER_PASSWORD_CHANGE = "user.password_change"  # self-service (actor == subject)
USER_PROFILE_UPDATE = "user.profile_update"  # self-service name edit (PATCH /me)
USER_PASSWORD_RESET = "user.password_reset"  # admin resets ANOTHER user
USER_PASSWORD_RESET_REQUEST = "user.password_reset_request"  # forgot-password mint
USER_PASSWORD_RESET_CONFIRM = "user.password_reset_confirm"  # forgot-password consume


@dataclass(frozen=True)
class CreatedUser:
    user: User
    initial_password: str


def generate_initial_password() -> str:
    """A cryptographically secure, one-time initial password.

    `secrets.token_urlsafe(18)` yields ~24 URL-safe base64 characters (108
    bits of entropy) — comfortably clears any password-strength bar without
    needing to run it through `django.contrib.auth.password_validation`
    (irrelevant here: `set_password` is called directly, the same
    system-generated-password path every other part of this codebase that
    creates a `User` already uses, e.g. `apps.accounts.managers.UserManager`).
    """
    return secrets.token_urlsafe(18)


def create_user_with_generated_password(*, email: str, name: str) -> CreatedUser:
    """Create a new `User` in the CURRENT tenant (from
    `apps.tenancy.context`, never client input — R4), with a random initial
    password.

    Tenant is derived from the ambient tenant context, the same way
    `apps.rbac.models.Membership.save()` derives `tenant` from `user` rather
    than trusting caller input. Callers must already be inside an
    authenticated request (`CurrentTenantMiddleware` has entered
    `tenant_context()`) or an explicit `tenant_context()` block (tests,
    management commands).

    Email uniqueness (per-tenant, per `docs/data-model.md`) is NOT
    re-validated here — the caller (`CreateUserSerializer.validate_email`)
    already checked it via the tenant-scoped `User.objects` manager and
    raised a clean `ValidationError` if it collided; this function still
    relies on the DB's `uniq_user_tenant_email` constraint as the final
    backstop against a race, same as every other tenant-scoped uniqueness
    check in this codebase (see `apps.common.errors._integrity_error_response`).
    """
    tenant_id = get_current_tenant_id()
    if tenant_id is None:
        raise TenantContextError("No tenant in context; refusing to create a user.")

    password = generate_initial_password()
    user = User(
        tenant_id=tenant_id,
        email=User.all_objects.normalize_email(email),
        name=name,
    )
    user.set_password(password)
    user.save()
    return CreatedUser(user=user, initial_password=password)


def validate_new_password(user: User, new_password: str) -> None:
    """Run a USER-CHOSEN password through Django's `AUTH_PASSWORD_VALIDATORS`
    (length/common/numeric/similarity — the last needs the `User`). Raises
    `django.core.exceptions.ValidationError` on a weak password; the caller
    maps it to an RFC-7807 400. Split out from `set_user_password` so the
    forgot-password confirm can validate BEFORE consuming its single-use token.
    """
    validate_password(new_password, user=user)


def set_user_password(user: User, new_password: str) -> None:
    """Validate and set a USER-CHOSEN password (self-service change / forgot-
    password confirm), then persist it.

    Unlike `create_user_with_generated_password` (a CSPRNG value that skips the
    validators by design), a client-supplied password MUST clear the validators
    (`validate_new_password`) — this is the single choke point that runs them.
    `set_password` re-hashes with Argon2 (same hasher as everywhere else); the
    plaintext is never persisted or logged.
    """
    validate_new_password(user, new_password)
    user.set_password(new_password)
    user.save(update_fields=["password", "updated_at"])


def reset_user_password(user: User) -> str:
    """Admin action: regenerate `user`'s password to a fresh CSPRNG one-time
    value and return the plaintext ONCE (same handling contract as
    `create_user_with_generated_password`'s `initial_password` — the caller
    puts it in the HTTP body exactly once and it never touches the audit log).

    Callers must already be inside the target user's tenant context (the
    `user.manage`-gated viewset action resolves `user` through the tenant-
    scoped manager first).
    """
    password = generate_initial_password()
    user.set_password(password)
    user.save(update_fields=["password", "updated_at"])
    return password


# --- Forgot-password reset tokens ------------------------------------------


def _hash_reset_token(raw_token: str) -> str:
    """SHA-256 hex digest of the raw token. The DB only ever stores this — the
    raw token exists only in the emailed link (same posture as Django's
    session-key hashing)."""
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def _reset_token_ttl() -> timedelta:
    return timedelta(seconds=settings.PASSWORD_RESET_TOKEN_TTL_SECONDS)


def create_password_reset_token(user: User) -> str:
    """Mint a one-time reset token for `user` and return the RAW token (to be
    emailed, never stored). Any of the user's still-valid outstanding tokens
    are invalidated first, so at most one link is ever live.

    Must run inside `user`'s tenant context (the caller resolves the tenant
    from the request's `tenant` slug and enters `tenant_context()` first — the
    same reviewed R4 exception `LoginView` uses).
    """
    now = timezone.now()
    # Invalidate any live tokens for this user (single-active-link invariant).
    PasswordResetToken.objects.filter(user=user, used_at__isnull=True).update(used_at=now)

    raw_token = secrets.token_urlsafe(32)
    PasswordResetToken.objects.create(
        tenant_id=user.tenant_id,
        user=user,
        token_hash=_hash_reset_token(raw_token),
        expires_at=now + _reset_token_ttl(),
    )
    return raw_token


def peek_live_reset_token(raw_token: str) -> PasswordResetToken | None:
    """Return the live (unused, unexpired) token matching `raw_token`'s hash
    within the CURRENT tenant context, WITHOUT consuming it — so the caller can
    validate the proposed new password before committing the single use.
    Returns `None` if no live token matches.

    Uses the tenant-scoped `.objects` manager: the caller has already resolved
    and entered the token's tenant context, so RLS + the app filter both scope
    this lookup to that tenant (no cross-tenant token is ever visible).
    """
    return (
        PasswordResetToken.objects.select_related("user")
        .filter(
            token_hash=_hash_reset_token(raw_token),
            used_at__isnull=True,
            expires_at__gt=timezone.now(),
        )
        .first()
    )


def claim_reset_token(token: PasswordResetToken) -> bool:
    """Atomically mark `token` used, but only if it is still unused. Returns
    `True` if THIS call won the claim, `False` if it was already consumed
    (concurrent confirm) — the conditional `UPDATE ... WHERE used_at IS NULL`
    makes the single-use guarantee race-safe rather than relying on the earlier
    read."""
    updated = PasswordResetToken.objects.filter(pk=token.pk, used_at__isnull=True).update(
        used_at=timezone.now()
    )
    return updated == 1
