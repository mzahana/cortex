"""Serializer for the `Membership` admin endpoint (T5.3 audit-finalize gap:
`docs/api-and-ui.md` documents `GET/POST /api/v1/memberships` -- "Assign
role/scope" -- and `docs/rbac.md` §5 requires every `user.manage`/
`role.assign` action to write an `AuditLog` entry, but no M0-M4 task ever
actually built this endpoint (grepped: no ViewSet/serializer/URL existed
anywhere in the tree before this task). Built here, minimally, so
"role change" (F8's acceptance wording) has an actual mutation site to audit
-- see `apps.rbac.api.MembershipViewSet`.

Tenant-scoping note (same pattern as `apps.catalog.serializers`): `user`/
`role`/`project` are `PrimaryKeyRelatedField`s built from each model's
tenant-scoped `.objects` manager, resolved lazily in `get_fields()` (never at
class-body/import time, which would raise `TenantContextError` at Django
startup) -- this is what makes a client-supplied id from another tenant
simply never resolve (R4), rather than needing a separate cross-tenant check.
"""

from __future__ import annotations

from rest_framework import serializers

from apps.accounts.models import User
from apps.projects.models import Project

from .models import Membership, Role


class MembershipSerializer(serializers.ModelSerializer):
    user_email = serializers.CharField(source="user.email", read_only=True)
    role_key = serializers.CharField(source="role.key", read_only=True)
    role_name = serializers.CharField(source="role.name", read_only=True)
    project_name = serializers.CharField(source="project.name", read_only=True, default=None)

    class Meta:
        model = Membership
        fields = [
            "id",
            "user",
            "user_email",
            "role",
            "role_key",
            "role_name",
            "project",
            "project_name",
            "created_at",
        ]
        read_only_fields = ["id", "created_at"]

    def get_fields(self):
        fields = super().get_fields()
        # Deferred to request time -- see module docstring.
        fields["user"].queryset = User.objects.all()  # type: ignore[attr-defined]
        fields["role"].queryset = Role.objects.all()  # type: ignore[attr-defined]
        fields["project"].queryset = Project.objects.all()  # type: ignore[attr-defined]
        fields["project"].required = False
        fields["project"].allow_null = True
        if self.instance is not None:
            # `PATCH` on an existing Membership is a **role change** only
            # (F8's exact wording) -- `user`/`project` are fixed at create
            # time; moving a membership between users/projects is "remove +
            # re-add", not an in-place edit, keeping the audit trail
            # unambiguous (one entry per intent, `apps.rbac.api` module
            # docstring).
            fields["user"].read_only = True
            fields["project"].read_only = True
        return fields

    def validate(self, attrs):
        # `uniq_membership_user_role_project` (T0.4) has no serializer-level
        # check otherwise -- same "no bare IntegrityError" finding
        # `apps.catalog.serializers` already fixed for Category/Location/
        # Project.
        user = attrs.get("user", getattr(self.instance, "user", None))
        role = attrs.get("role", getattr(self.instance, "role", None))
        project = attrs.get("project", getattr(self.instance, "project", None))
        qs = Membership.objects.filter(user=user, role=role, project=project)
        if self.instance is not None:
            qs = qs.exclude(pk=self.instance.pk)  # type: ignore[union-attr]
        if qs.exists():
            raise serializers.ValidationError("This user already holds this role in this scope.")
        return attrs
