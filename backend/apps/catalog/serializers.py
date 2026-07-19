"""Serializers for the catalog admin-config endpoints (T1.1).

Tenant scoping note: every `PrimaryKeyRelatedField`/`SlugRelatedField` that
points at another tenant-owned model (`Category.parent`, `Location.parent`,
`CustomFieldDef.category`, `Project.lead_user`) is built with a queryset from
that model's tenant-scoped `.objects` manager, resolved lazily per-request in
`get_fields()` rather than at class-body/import time. `Model.objects.all()`
(the fail-closed `TenantScopedManager`, T0.4) raises `TenantContextError` the
instant it's evaluated with no tenant in context — which is exactly what
would happen at Django *startup* (module import) if the queryset were built
in the class body, long before any request/tenant context exists. Building it
in `get_fields()` defers evaluation until a real request is being served
(tenant context already pushed by `CurrentTenantMiddleware`), and also
guarantees a client can never select a *different* tenant's row via a raw id
in the request body (R4/F1 — the carried finding this pattern specifically
exists for, since `Project.lead_user` now references `accounts.User`, whose
default `.objects` manager would otherwise be reachable unscoped via
`Meta.default_manager_name = "all_objects"`).
"""

from __future__ import annotations

from typing import Any

from rest_framework import serializers

from apps.accounts.models import User
from apps.projects.models import Project

from .models import Category, CustomFieldDef, Location, Tag


def _validate_no_tree_cycle(instance: Any, parent: Any, *, field_name: str = "parent") -> None:
    """Code-review finding (T1.1 follow-up): `instance.parent = instance` was
    the only cycle blocked before; a longer cycle (`A.parent = B` then
    `B.parent = A`, or deeper) was not, which would infinite-loop any
    recursive tree/breadcrumb traversal (T1.5 builds exactly that). Walk up
    from the PROPOSED `parent`'s already-saved ancestor chain; if that walk
    ever reaches `instance` itself, assigning `parent` would close a cycle
    (`instance` would become an ancestor of its own ancestor). Covers both
    the direct case (`node is instance` on the first step, when `parent is
    instance`) and any longer one, for both `Category` and `Location`.
    """
    if parent is None or instance is None:
        return
    node = parent
    seen_ids: set[Any] = set()
    while node is not None:
        if node.pk == instance.pk:
            raise serializers.ValidationError(
                {field_name: "This assignment would create a cycle in the tree."}
            )
        if node.pk in seen_ids:
            # An already-existing cycle elsewhere in the data (should be
            # unreachable given this validator runs on every write) — stop
            # rather than loop forever.
            break
        seen_ids.add(node.pk)
        node = node.parent


class CustomFieldDefSerializer(serializers.ModelSerializer):
    class Meta:
        model = CustomFieldDef
        fields = [
            "id",
            "category",
            "key",
            "label",
            "data_type",
            "unit",
            "enum_options",
            "required",
            "order",
            "created_at",
        ]
        # `category` is derived from the URL (`/categories/{id}/fields`, see
        # `apps.catalog.api.CategoryViewSet.fields`), never trusted from the
        # request body — same "don't trust a client-supplied FK, re-scope it"
        # rule the add-endpoint skill calls out for foreign keys in general.
        # Read-only here also sidesteps needing a tenant-scoped queryset for
        # it in this serializer (no client-writable value to validate).
        read_only_fields = ["id", "category", "created_at"]

    def validate(self, attrs):
        data_type = attrs.get("data_type", getattr(self.instance, "data_type", None))
        enum_options = attrs.get("enum_options", getattr(self.instance, "enum_options", None))
        if data_type == CustomFieldDef.DataType.ENUM and not enum_options:
            raise serializers.ValidationError(
                {"enum_options": "enum_options is required when data_type='enum'."}
            )

        # Edit policy (M1 follow-up, docs/api-and-ui.md "Custom field def
        # edit/delete policy"): changing `data_type` on a field def that
        # already has stored `AssetFieldValue` rows could orphan/invalidate
        # those values (an int-coerced value is nonsensical once the field
        # becomes `date`), so `data_type` changes are BLOCKED once ANY value
        # exists for this field. Every other attribute (`label`, `unit`,
        # `enum_options`, `required`, `order`) stays freely editable at any
        # time -- purely cosmetic/soft constraints that never invalidate
        # already-stored JSON. Local import: `apps.assets` imports
        # `apps.catalog.models.CustomFieldDef`, so importing `apps.assets` at
        # module level here would be a circular import.
        if (
            self.instance is not None
            and "data_type" in attrs
            and attrs["data_type"] != getattr(self.instance, "data_type", None)
        ):
            from apps.assets.models import AssetFieldValue

            if AssetFieldValue.objects.filter(field_def=self.instance).exists():
                raise serializers.ValidationError(
                    {
                        "data_type": "Cannot change data_type: this field already has stored "
                        "values on one or more assets. Delete the field (which removes those "
                        "values) or clear them first."
                    }
                )

        # Code-review finding: `(tenant, category, key)` is a DB
        # `UniqueConstraint` with no serializer-level check, so a duplicate
        # key under the same category previously hit `IntegrityError` -> an
        # unhandled 500 instead of a clean 400. `category` is read-only here
        # (derived from the URL, never trusted from the body — see the
        # `Meta.read_only_fields` comment above) so it is never in `attrs`;
        # the view passes it through `context["category"]` instead (see
        # `apps.catalog.api.CategoryViewSet.fields`).
        category = self.context.get("category") or getattr(self.instance, "category", None)
        key = attrs.get("key", getattr(self.instance, "key", None))
        if category is not None and key:
            qs = CustomFieldDef.objects.filter(category=category, key=key)
            if self.instance is not None:
                qs = qs.exclude(pk=self.instance.pk)  # type: ignore[union-attr]
            if qs.exists():
                raise serializers.ValidationError(
                    {"key": "A custom field with this key already exists for this category."}
                )
        return attrs


class CategorySerializer(serializers.ModelSerializer):
    field_defs = CustomFieldDefSerializer(many=True, read_only=True)

    class Meta:
        model = Category
        fields = [
            "id",
            "name",
            "parent",
            "default_is_consumable",
            "requires_approval",
            "requires_calibration",
            "field_defs",
            "created_at",
        ]
        read_only_fields = ["id", "created_at"]

    def get_fields(self):
        fields = super().get_fields()
        # Tenant-scoped self-referential parent, resolved per-request. mypy's
        # generic `Field` stub has no `queryset` attribute (only
        # `RelatedField` subclasses do at runtime) — see module docstring for
        # why this must be set here rather than in the class body.
        fields["parent"].queryset = Category.objects.all()  # type: ignore[attr-defined]
        return fields

    def validate_parent(self, parent: Category | None) -> Category | None:
        _validate_no_tree_cycle(self.instance, parent)
        return parent

    def validate(self, attrs):
        # Code-review finding: `(tenant, parent, name)` is a DB
        # `UniqueConstraint` with no serializer-level check, so a duplicate
        # name under the same parent previously hit `IntegrityError` -> an
        # unhandled 500 instead of a clean 400 on the `name` field.
        name = attrs.get("name", getattr(self.instance, "name", None))
        parent = attrs["parent"] if "parent" in attrs else getattr(self.instance, "parent", None)
        qs = Category.objects.filter(parent=parent, name=name)
        if self.instance is not None:
            qs = qs.exclude(pk=self.instance.pk)  # type: ignore[union-attr]
        if qs.exists():
            raise serializers.ValidationError(
                {"name": "A category with this name already exists under the same parent."}
            )
        return attrs


class LocationSerializer(serializers.ModelSerializer):
    class Meta:
        model = Location
        fields = ["id", "name", "parent", "kind", "created_at"]
        read_only_fields = ["id", "created_at"]

    def get_fields(self):
        fields = super().get_fields()
        fields["parent"].queryset = Location.objects.all()  # type: ignore[attr-defined]
        return fields

    def validate_parent(self, parent: Location | None) -> Location | None:
        _validate_no_tree_cycle(self.instance, parent)
        return parent

    def validate(self, attrs):
        name = attrs.get("name", getattr(self.instance, "name", None))
        parent = attrs["parent"] if "parent" in attrs else getattr(self.instance, "parent", None)
        qs = Location.objects.filter(parent=parent, name=name)
        if self.instance is not None:
            qs = qs.exclude(pk=self.instance.pk)  # type: ignore[union-attr]
        if qs.exists():
            raise serializers.ValidationError(
                {"name": "A location with this name already exists under the same parent."}
            )
        return attrs


class ProjectSerializer(serializers.ModelSerializer):
    class Meta:
        model = Project
        fields = ["id", "name", "lead_user", "is_active", "created_at"]
        read_only_fields = ["id", "created_at"]

    def get_fields(self):
        fields = super().get_fields()
        # R4/F1 (carried finding): `User`'s default manager is deliberately
        # scoped (`User.objects` is `TenantScopedManager`, see
        # `apps.accounts.models.User`) — using it here, rather than the
        # unscoped `User.all_objects`, is what makes `lead_user` only ever
        # selectable from the caller's own tenant, never any other tenant's
        # users. Resolved lazily (see module docstring) so it is safe to
        # evaluate under the request's real tenant context.
        fields["lead_user"].queryset = User.objects.all()  # type: ignore[attr-defined]
        return fields

    def validate(self, attrs):
        # Same DB-`UniqueConstraint`-without-serializer-check finding as
        # Category/Location above, for `(tenant, name)`.
        name = attrs.get("name", getattr(self.instance, "name", None))
        qs = Project.objects.filter(name=name)
        if self.instance is not None:
            qs = qs.exclude(pk=self.instance.pk)  # type: ignore[union-attr]
        if qs.exists():
            raise serializers.ValidationError({"name": "A project with this name already exists."})
        return attrs


class TagSerializer(serializers.ModelSerializer):
    class Meta:
        model = Tag
        fields = ["id", "name", "created_at"]
        read_only_fields = ["id", "name", "created_at"]
