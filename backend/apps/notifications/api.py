"""`GET/PATCH /api/v1/notification-prefs` (T5.1, `docs/api-and-ui.md`).

Minimal, self-scoped surface: a user can list and toggle THEIR OWN
`NotificationPref` rows only -- never another user's (tenant isolation
golden-path step 1 is already satisfied by the tenant-scoped `.objects`
manager; step 2, RBAC, is `NotifySelfPermission`; step 3, own-row-only, is
`get_queryset`/`get_object` below never resolving another user's row
regardless of what a client requests).

`PATCH /notification-prefs/{event_type}/` upserts: a user with no explicit
preference yet for `event_type` defaults to enabled (opt-out semantics,
`apps.notifications.services.enqueue_transactional_email` docstring), so
the first PATCH for a never-before-seen `event_type` creates the row rather
than 404ing. Richer UX (e.g. a fixed list of known event types even before
a user has an explicit row for each) is T5.6's concern.
"""

from __future__ import annotations

from rest_framework import mixins, viewsets

from .models import NotificationPref
from .permissions import NotifySelfPermission
from .serializers import NotificationPrefSerializer


class NotificationPrefViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.UpdateModelMixin,
    viewsets.GenericViewSet,
):
    serializer_class = NotificationPrefSerializer
    permission_classes = [NotifySelfPermission]
    lookup_field = "event_type"
    lookup_value_regex = r"[^/]+"
    http_method_names = ["get", "patch", "head", "options"]

    def get_queryset(self):
        # Tenant-scoped manager (golden path step 1) + own-user-only filter
        # (step 3) -- resolved per-request, never a class-level queryset.
        return NotificationPref.objects.filter(user=self.request.user).order_by("event_type")

    def get_object(self):
        event_type = self.kwargs[self.lookup_field]
        obj, _ = NotificationPref.objects.get_or_create(
            tenant=self.request.user.tenant,
            user=self.request.user,
            event_type=event_type,
            defaults={"email_enabled": True},
        )
        self.check_object_permissions(self.request, obj)
        return obj
