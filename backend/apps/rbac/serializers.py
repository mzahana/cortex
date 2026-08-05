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

from typing import cast

from django.utils.text import slugify
from rest_framework import serializers

from apps.accounts.models import User
from apps.projects.models import Project

from .models import Membership, Permission, Role, RolePermission, UserPermissionOverride
from .permission_keys import PERMISSION_LABELS


class PermissionSerializer(serializers.ModelSerializer):
    """`GET /api/v1/permissions` — the fixed, system-wide permission
    vocabulary (`apps.rbac.permission_keys.PERMISSION_LABELS`), so the admin
    UI can render a checkbox matrix without hard-coding the key list in the
    frontend. `group` is derived from the key's dotted prefix (`asset.create`
    -> `asset`) purely for UI sectioning — it is not a stored column and
    carries no authorization meaning."""

    group = serializers.SerializerMethodField()

    class Meta:
        model = Permission
        fields = ["id", "key", "label", "group"]
        read_only_fields = fields

    def get_group(self, obj: Permission) -> str:
        return obj.key.split(".", 1)[0]


class RoleSerializer(serializers.ModelSerializer):
    """`GET/POST /api/v1/roles`, `PATCH/DELETE /api/v1/roles/{id}`.

    `permission_keys` is the role's full grant set, and it is WRITABLE
    (docs/rbac.md §6): sending it REPLACES the role's grants wholesale — the
    natural shape for a checkbox matrix, where "unchecked" has to mean
    "revoked", not "omitted". `apps.rbac.api.RoleViewSet` is what enforces
    that only a tenant-wide `tenant.manage` holder may write it, and audits
    the before/after key sets.

    Writing `permission_keys` on a SYSTEM role is allowed (that is the whole
    point of the feature — "Project Leads still can't add categories") and
    flips `is_customized`, which is what stops `apps.rbac.seed.
    seed_roles_for_tenant` from silently re-adding the defaults later.
    """

    permission_keys = serializers.ListField(
        child=serializers.CharField(max_length=64), required=False, allow_empty=True
    )
    # Plain `CharField`, NOT the model's `SlugField`: the UI sends a human
    # name ("Lab Tech") and `validate_key` slugifies it. A `SlugField` would
    # run its own regex validator FIRST and 400 before slugification ever
    # happened.
    key = serializers.CharField(max_length=50, required=False)
    member_count = serializers.SerializerMethodField(read_only=True)

    class Meta:
        model = Role
        fields = [
            "id",
            "key",
            "name",
            "is_system",
            "is_customized",
            "permission_keys",
            "member_count",
        ]
        read_only_fields = ["id", "is_system", "is_customized", "member_count"]

    def get_member_count(self, obj: Role) -> int:
        # Annotated by `RoleViewSet.get_queryset`; falls back to a count for
        # the post-write serialization path (single object, no N+1 risk).
        annotated = getattr(obj, "membership_count", None)
        return annotated if annotated is not None else obj.memberships.count()

    def to_representation(self, instance: Role) -> dict:
        data = super().to_representation(instance)
        data["permission_keys"] = sorted(
            rp.permission.key for rp in instance.role_permissions.all()
        )
        return data

    def validate_permission_keys(self, value: list[str]) -> list[str]:
        unknown = sorted(set(value) - set(PERMISSION_LABELS))
        if unknown:
            raise serializers.ValidationError(
                f"Unknown permission key(s): {', '.join(unknown)}. The permission "
                "vocabulary is fixed — see GET /api/v1/permissions."
            )
        return sorted(set(value))

    def validate_key(self, value: str) -> str:
        slug = slugify(value or "")
        if not slug:
            raise serializers.ValidationError("Provide a key (letters, digits, dashes).")
        return slug

    def validate(self, attrs: dict) -> dict:
        # `key` is optional on create: the UI collects a human name ("Lab
        # Tech") and nothing else, so derive the slug from it rather than
        # letting a blank `key` reach the DB.
        if self.instance is None and not attrs.get("key"):
            derived = slugify(attrs.get("name", "") or "")
            if not derived:
                raise serializers.ValidationError({"name": "Provide a role name."})
            attrs["key"] = derived

        # `uniq_role_tenant_key` has no serializer-level check otherwise —
        # same "no bare IntegrityError" rule `MembershipSerializer.validate`
        # follows.
        key = attrs.get("key", getattr(self.instance, "key", None))
        if key is not None:
            qs = Role.objects.filter(key=key)
            if self.instance is not None:
                # `ModelSerializer.instance` is typed `_MT | Sequence[_MT] |
                # None` to cover `many=True` list serializers, but
                # `RoleViewSet` (a plain `ModelViewSet`) never constructs this
                # serializer with `many=True` -- a non-None `self.instance`
                # here is always the single `Role` being updated.
                instance = cast(Role, self.instance)
                qs = qs.exclude(pk=instance.pk)
            if qs.exists():
                raise serializers.ValidationError({"key": "A role with this key already exists."})
        if self.instance is not None and cast(Role, self.instance).is_system and "key" in attrs:
            # Renaming a system role's KEY would break
            # `SYSTEM_ROLE_PERMISSIONS`/`DEFAULT_ROLE_KEY` lookups (and the
            # "assign Member only" ProjectLead rule keys off `role.key`).
            # The display `name` stays editable.
            raise serializers.ValidationError({"key": "A system role's key cannot be changed."})
        return attrs

    def create(self, validated_data: dict) -> Role:
        permission_keys = validated_data.pop("permission_keys", [])
        role = Role.objects.create(is_system=False, **validated_data)
        apply_role_permissions(role, permission_keys)
        return role

    def update(self, instance: Role, validated_data: dict) -> Role:
        permission_keys = validated_data.pop("permission_keys", None)
        for field, value in validated_data.items():
            setattr(instance, field, value)
        if permission_keys is not None:
            apply_role_permissions(instance, permission_keys)
            instance.is_customized = True
        instance.save()
        return instance


def apply_role_permissions(role: Role, permission_keys: list[str]) -> None:
    """Replace `role`'s grants with exactly `permission_keys` (already
    validated against the fixed vocabulary). Diff-based rather than
    delete-all-then-recreate so the rows that survive keep their identity
    (and so a partial failure can't leave a role momentarily permission-less
    for a concurrent request)."""
    permissions_by_key = {p.key: p for p in Permission.objects.filter(key__in=permission_keys)}
    wanted = set(permissions_by_key)
    existing = {rp.permission.key: rp for rp in role.role_permissions.select_related("permission")}

    for key in sorted(set(existing) - wanted):
        existing[key].delete()
    for key in sorted(wanted - set(existing)):
        RolePermission.objects.create(
            tenant_id=role.tenant_id, role=role, permission=permissions_by_key[key]
        )


class UserPermissionOverrideSerializer(serializers.ModelSerializer):
    permission_key = serializers.CharField(source="permission.key", read_only=True)

    class Meta:
        model = UserPermissionOverride
        fields = ["id", "permission", "permission_key", "effect", "created_at"]
        read_only_fields = fields


class UserPermissionsWriteSerializer(serializers.Serializer):
    """Body of `PUT /api/v1/users/{id}/permissions` — the WHOLE override set
    for that user, as `{permission_key: "grant"|"deny"}`. A replace (not a
    patch) for the same reason `RoleSerializer.permission_keys` is: the UI
    is a tri-state matrix (inherit / always allow / never allow), and
    "back to inherit" has to be expressible by omission."""

    overrides = serializers.DictField(child=serializers.CharField(), allow_empty=True)

    def validate_overrides(self, value: dict) -> dict:
        unknown = sorted(set(value) - set(PERMISSION_LABELS))
        if unknown:
            raise serializers.ValidationError(f"Unknown permission key(s): {', '.join(unknown)}.")
        valid_effects = {c[0] for c in UserPermissionOverride.Effect.choices}
        bad = sorted({k for k, v in value.items() if v not in valid_effects})
        if bad:
            raise serializers.ValidationError(
                f"Effect must be one of {sorted(valid_effects)} (bad: {', '.join(bad)})."
            )
        return value


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
