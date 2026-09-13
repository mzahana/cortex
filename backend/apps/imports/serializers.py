"""Serializers for `apps.imports` (T6.1)."""

from __future__ import annotations

import json
from typing import Any

from rest_framework import serializers

from apps.jobs.models import Job

from .models import ImportJob
from .services import (
    CREATABLE_TARGETS,
    DEFAULT_ON_DUPLICATE,
    ON_DUPLICATE_CHOICES,
    SUPPORTED_EXTENSIONS,
    file_extension,
)


class MappingField(serializers.Field):
    """A `{header: target}` mapping override, accepted EITHER as a real JSON
    object (a plain `application/json` request body — `POST /imports/{id}/
    commit` normally has no file, so it can use a real JSON body) OR as a
    JSON-encoded STRING form field (multipart requests — `POST /imports`
    always is one, since it also carries the uploaded `file`; DRF's own
    `JSONField` does NOT auto-parse a string value from a multipart form
    the way it does for `JSONParser`-decoded bodies, so a plain `JSONField`
    here would silently pass the raw string through instead of the dict the
    rest of this app expects).
    """

    def to_internal_value(self, data: Any) -> dict[str, str]:
        if data in (None, ""):
            return {}
        if isinstance(data, dict):
            mapping = data
        elif isinstance(data, (str, bytes)):
            try:
                mapping = json.loads(data)
            except (TypeError, ValueError):
                raise serializers.ValidationError("'mapping' must be valid JSON.") from None
        else:
            raise serializers.ValidationError("'mapping' must be a JSON object.")
        if not isinstance(mapping, dict):
            raise serializers.ValidationError("'mapping' must be a JSON object.")
        return {str(k): str(v) for k, v in mapping.items()}

    def to_representation(self, value: Any) -> Any:
        return value


class CreateMissingField(serializers.Field):
    """The `create_missing` opt-in: which of `category`/`location`/`project`
    the importer may CREATE when a row names one the tenant doesn't have
    (`apps.imports.services`'s module docstring).

    Accepted in the same two shapes as `MappingField`, and for the same
    reason — a real JSON array on the JSON-bodied commit endpoint, or a
    JSON-encoded string field on the multipart upload. A bare `true` is
    also accepted as "all three", since that's the obvious thing a client
    sends for a single "create anything missing" checkbox.
    """

    def to_internal_value(self, data: Any) -> list[str]:
        if data in (None, "", False):
            return []
        if data is True or data in ("true", "True"):
            return sorted(CREATABLE_TARGETS)
        if isinstance(data, (str, bytes)):
            try:
                data = json.loads(data)
            except (TypeError, ValueError):
                raise serializers.ValidationError("'create_missing' must be valid JSON.") from None
        if data is True:
            return sorted(CREATABLE_TARGETS)
        if not isinstance(data, (list, tuple)):
            raise serializers.ValidationError(
                "'create_missing' must be a list of " f"{', '.join(sorted(CREATABLE_TARGETS))}."
            )
        targets = [str(item) for item in data]
        unknown = sorted(set(targets) - CREATABLE_TARGETS)
        if unknown:
            raise serializers.ValidationError(
                f"Unknown create_missing target(s): {', '.join(unknown)}. "
                f"Allowed: {', '.join(sorted(CREATABLE_TARGETS))}."
            )
        return sorted(set(targets))

    def to_representation(self, value: Any) -> Any:
        return value


def _on_duplicate_field():
    """`on_duplicate` — what to do with a row that names an asset the tenant
    already has (`apps.imports.services.annotate_duplicates`). A plain
    `ChoiceField` works in both request shapes (multipart form value and
    JSON string alike), unlike `mapping`/`create_missing`, because the value
    is a bare string rather than JSON.
    """
    return serializers.ChoiceField(
        choices=sorted(ON_DUPLICATE_CHOICES),
        required=False,
        default=DEFAULT_ON_DUPLICATE,
    )


class ImportJobJobSerializer(serializers.ModelSerializer):
    """Minimal nested view of a `Job` (id/status/error) — the client polls
    the FULL job (including `download_url`, unused here) via the existing
    `GET /api/v1/jobs/{id}` (T4.5), this is just enough to know which job id
    to poll and its last-known status without a second round trip.
    """

    class Meta:
        model = Job
        fields = ["id", "status", "error"]
        read_only_fields = fields


class ImportJobSerializer(serializers.ModelSerializer):
    dry_run_job = ImportJobJobSerializer(read_only=True)
    commit_job = ImportJobJobSerializer(read_only=True)

    class Meta:
        model = ImportJob
        fields = [
            "id",
            "status",
            "source_filename",
            "mapping",
            "report",
            "created_asset_ids",
            "dry_run_job",
            "commit_job",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields


class ImportUploadRequestSerializer(serializers.Serializer):
    """`POST /api/v1/imports` request body (multipart): `file` (required,
    `.csv`/`.xlsx`) + an optional `mapping` override, a JSON object string
    `{"<spreadsheet header>": "<target>"}` — see `apps.imports.services`
    module docstring for the target vocabulary. Any header omitted from
    `mapping` falls back to the auto-detected default.
    """

    file = serializers.FileField()
    mapping = MappingField(required=False, allow_null=True)
    create_missing = CreateMissingField(required=False, allow_null=True)
    on_duplicate = _on_duplicate_field()

    def validate_file(self, uploaded_file):
        ext = file_extension(uploaded_file.name or "")
        if ext not in SUPPORTED_EXTENSIONS:
            raise serializers.ValidationError(
                f"Unsupported file extension '.{ext}'. Only .csv and .xlsx are supported."
            )
        return uploaded_file


class ImportCommitRequestSerializer(serializers.Serializer):
    """`POST /api/v1/imports/{id}/commit` request body: entirely optional —
    absent/empty re-uses the `ImportJob`'s already-confirmed `mapping`
    (whatever the last dry-run/commit resolved). A caller MAY override it
    (e.g. the UI lets a user tweak a mapping without a full dry-run round
    trip) — the commit task re-validates from scratch either way, so a bad
    override still surfaces as a `commit_failed` report, never silently
    creates wrong assets.
    """

    mapping = MappingField(required=False, allow_null=True)
    create_missing = CreateMissingField(required=False, allow_null=True)
    on_duplicate = _on_duplicate_field()
