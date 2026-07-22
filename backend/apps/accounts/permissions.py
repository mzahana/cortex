"""DRF permission class for `GET/POST /api/v1/users` (the "create/discover a
user" gap fill — see `apps.accounts.services` module docstring for the full
gap analysis).

Same two-phase-pattern *spirit* as `apps.rbac.permissions.MembershipPermission`
(this endpoint has no per-object action, so there is only one phase here):

- `create` (`POST /api/v1/users`) — Admin-only: requires a TENANT-WIDE grant
  of `user.manage`. Mirrors `MembershipPermission`'s create-time Admin gate
  for brand-new accounts; `docs/rbac.md` footnote 3 scopes a ProjectLead's
  `user.manage` grant to *adding an existing user to their project*, never to
  minting new accounts tenant-wide.
- `list` (`GET /api/v1/users`) — any scope: a ProjectLead whose ONLY grant of
  `user.manage` is project-scoped must still be able to find an existing
  user's id (there is no other way to discover one before using
  `POST /api/v1/memberships`, whose `user` field requires an id already).
"""

from __future__ import annotations

from rest_framework.permissions import BasePermission

from apps.rbac.permission_keys import USER_MANAGE
from apps.rbac.services import user_has_permission, user_has_permission_in_any_scope

USER_ACTION_PERMISSION_MAP: dict[str, str] = {
    "list": USER_MANAGE,
    "create": USER_MANAGE,
}


class UserManagementPermission(BasePermission):
    def has_permission(self, request, view) -> bool:
        user = getattr(request, "user", None)
        if user is None or not user.is_authenticated:
            return False

        action = getattr(view, "action", "") or ""
        if action not in USER_ACTION_PERMISSION_MAP:
            return False  # fail-closed: unmapped action

        if action == "create":
            # Admin-only: `project=None` only matches a TENANT-WIDE
            # membership's grant (see `apps.rbac.services.
            # get_effective_permissions` — a project-scoped membership never
            # contributes when `project=None`), i.e. exactly the
            # `MembershipPermission` create-time Admin semantics.
            return user_has_permission(user, USER_MANAGE, project=None)

        # list: any scope (tenant-wide OR project-scoped) suffices.
        return user_has_permission_in_any_scope(user, USER_MANAGE)
