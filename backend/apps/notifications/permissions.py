"""RBAC for the `notification-prefs` endpoint (T5.1).

`notify.self` (`docs/rbac.md` §3) is granted tenant-wide to every role
(Admin/ProjectLead/Member/Viewer all show ✅), and is inherently
self-scoped -- there is no per-project variant of "configure my own
notifications". So this permission class only needs: authenticated +
holds `notify.self` (tenant-wide check, no project-scope resolution
needed), plus an object-level "it's your own row" check as defense in
depth (`get_object()` in `api.py` already only ever resolves rows scoped
to `request.user`, so this can never actually fail in practice -- but
costs nothing to assert explicitly, same posture as other permission
classes in this codebase).
"""

from __future__ import annotations

from rest_framework.permissions import BasePermission

from apps.rbac.permission_keys import NOTIFY_SELF
from apps.rbac.services import user_has_permission


class NotifySelfPermission(BasePermission):
    def has_permission(self, request, view) -> bool:
        user = getattr(request, "user", None)
        if user is None or not user.is_authenticated:
            return False
        return user_has_permission(user, NOTIFY_SELF)

    def has_object_permission(self, request, view, obj) -> bool:
        return obj.user_id == request.user.id
