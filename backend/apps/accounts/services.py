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

import secrets
from dataclasses import dataclass

from apps.tenancy.context import TenantContextError, get_current_tenant_id

from .models import User


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
