"""Serializers for the Asset registry (T1.2).

Tenant scoping (R4/F1, same pattern as `apps.catalog.serializers`): every
writable relation that points at another tenant-owned model (`category`,
`project`, `location`, `current_workload_user`) gets its queryset built in
`get_fields()`, from that model's tenant-scoped `.objects` manager, resolved
lazily per-request — never a class-body `queryset=Model.objects.all()`
(which would evaluate the fail-closed manager at import time with no tenant
context yet and crash). `current_workload_user` is the literal case this
task calls out: it MUST use `User.objects` (tenant-scoped), never
`User.all_objects`.
"""

from __future__ import annotations

from typing import Any

from rest_framework import serializers

from apps.accounts.models import User
from apps.catalog.models import Category, Location, Tag
from apps.projects.models import Project

from .models import Asset, Attachment
from .services import replace_asset_field_values


class AttachmentSerializer(serializers.ModelSerializer):
    """Read-only: attachments are created via the dedicated upload action
    (`POST /api/v1/assets/{id}/attachments`), never through the asset
    serializer's own create/update. `storage_key` is exposed here
    deliberately — it is the ONLY thing persisted for the file; the client
    fetches bytes via `MEDIA_URL + storage_key` (nginx-served volume), never
    from this API response directly.
    """

    class Meta:
        model = Attachment
        fields = [
            "id",
            "kind",
            "storage_key",
            "filename",
            "content_type",
            "size",
            "uploaded_by",
            "created_at",
        ]
        read_only_fields = fields


class AssetSerializer(serializers.ModelSerializer):
    # Write-only: `Asset.tags` is a real M2M manager attribute (through
    # `TagLink`), so DRF's default attribute-lookup `to_representation()`
    # can't render it directly from a plain `ListField` of names (it would
    # try to iterate the manager itself, not `.all()`). Read-side rendering
    # is added explicitly in `to_representation()` below instead, keyed by
    # the same "tags" name (a `write_only` field is excluded from the
    # default readable-fields pass, so there's no clash).
    tags = serializers.ListField(
        child=serializers.CharField(max_length=100),
        required=False,
        allow_empty=True,
        write_only=True,
    )
    custom_field_values = serializers.DictField(required=False, allow_null=True, write_only=True)
    field_values = serializers.SerializerMethodField(read_only=True)
    attachments = AttachmentSerializer(many=True, read_only=True)

    class Meta:
        model = Asset
        fields = [
            "id",
            "uuid",
            "qr_token",
            "category",
            "name",
            "description",
            "is_consumable",
            "project",
            "serial_number",
            "manufacturer",
            "model",
            "location",
            "purchase_date",
            "purchase_cost",
            "currency",
            "warranty_expiry",
            "supplier",
            "status",
            "condition",
            "retired_at",
            "current_workload_user",
            "tags",
            "custom_field_values",
            "field_values",
            "attachments",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "uuid", "qr_token", "retired_at", "created_at", "updated_at"]

    def get_fields(self):
        fields = super().get_fields()
        # Tenant-scoped, resolved per-request (see module docstring) — R4/F1.
        fields["category"].queryset = Category.objects.all()  # type: ignore[attr-defined]
        fields["project"].queryset = Project.objects.all()  # type: ignore[attr-defined]
        fields["location"].queryset = Location.objects.all()  # type: ignore[attr-defined]
        fields["current_workload_user"].queryset = User.objects.all()  # type: ignore[attr-defined]
        return fields

    def get_field_values(self, obj: Asset) -> dict[str, Any]:
        # `select_related("field_def")` — see `apps.assets.api` `get_queryset`
        # prefetch — avoids an N+1 here across a page of assets.
        return {fv.field_def.key: fv.value for fv in obj.field_values.all()}

    def to_representation(self, instance: Asset) -> dict[str, Any]:
        data = super().to_representation(instance)
        # See the `tags` field docstring above: rendered explicitly here
        # (from the `tag_links__tag` prefetch in `apps.assets.api.get_queryset`,
        # so no N+1) rather than via the default attribute-lookup path.
        data["tags"] = sorted(link.tag.name for link in instance.tag_links.all())
        return data

    def validate_category(self, category: Category) -> Category:
        return category

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        # `is_consumable` defaults from the category when not explicitly
        # supplied (docs/data-model.md §4) — only on CREATE; an edit that
        # omits it must not silently flip an existing asset's flag.
        if self.instance is None and "is_consumable" not in attrs:
            category = attrs.get("category")
            if category is not None:
                attrs["is_consumable"] = category.default_is_consumable

        # `status` is edit-able for ordinary lifecycle moves (available <->
        # in_use <-> maintenance <-> lost) but NOT to/from `retired` — that
        # transition only ever happens through the dedicated `retire` action
        # (audited, docs/rbac.md §5), never a plain PATCH.
        new_status = attrs.get("status")
        current_status = getattr(self.instance, "status", None)
        if new_status == Asset.Status.RETIRED and current_status != Asset.Status.RETIRED:
            raise serializers.ValidationError(
                {"status": "Use POST /assets/{id}/retire to retire an asset."}
            )
        still_retired = current_status == Asset.Status.RETIRED
        if still_retired and new_status not in (None, Asset.Status.RETIRED):
            raise serializers.ValidationError(
                {"status": "A retired asset cannot be edited back to another status here."}
            )
        return attrs

    def _sync_tags(self, asset: Asset, tag_names: list[str]) -> None:
        from .models import TagLink

        tenant = asset.tenant
        wanted = {name.strip() for name in tag_names if name.strip()}
        tags_by_name = {}
        for name in wanted:
            tag, _ = Tag.objects.get_or_create(tenant=tenant, name=name)
            tags_by_name[name] = tag
        TagLink.objects.filter(asset=asset).exclude(tag__name__in=wanted).delete()
        existing_names = set(
            TagLink.objects.filter(asset=asset).values_list("tag__name", flat=True)
        )
        for name, tag in tags_by_name.items():
            if name not in existing_names:
                TagLink.objects.create(tenant=tenant, asset=asset, tag=tag)

    def create(self, validated_data: dict[str, Any]) -> Asset:
        tag_names = validated_data.pop("tags", None)
        # `.pop(..., None) or {}`: on CREATE, "not supplied at all" and
        # "supplied as `{}`" are treated the same — required custom fields
        # for the category must always be satisfied when an asset is first
        # created (core F2 "required enforcement"), unlike a PATCH edit
        # (see `update()` below) which may legitimately leave custom fields
        # untouched.
        custom_field_values = validated_data.pop("custom_field_values", None) or {}
        request = self.context["request"]
        validated_data["tenant"] = request.user.tenant
        asset = Asset.objects.create(**validated_data)
        replace_asset_field_values(asset, asset.category, custom_field_values)
        if tag_names is not None:
            self._sync_tags(asset, tag_names)
        return asset

    def update(self, instance: Asset, validated_data: dict[str, Any]) -> Asset:
        tag_names = validated_data.pop("tags", None)
        custom_field_values = validated_data.pop("custom_field_values", None)
        for field, value in validated_data.items():
            setattr(instance, field, value)
        instance.save()
        category = validated_data.get("category", instance.category)
        # Only touch custom fields when the client's raw payload actually
        # included the key — distinguishing "omitted" (leave untouched) from
        # "explicitly sent `{}`" (re-validate against the FULL required set,
        # i.e. the client is intentionally clearing/replacing them) requires
        # looking at the raw `initial_data`, since a `DictField(required=
        # False)` drops the key from `validated_data` in both "omitted" and
        # (never, since `{}` is falsy-but-present) "empty" cases identically.
        if "custom_field_values" in self.initial_data:
            replace_asset_field_values(instance, category, custom_field_values or {})
        if tag_names is not None:
            self._sync_tags(instance, tag_names)
        return instance
