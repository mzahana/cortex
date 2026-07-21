"""`Job` read serializer (T4.5)."""

from __future__ import annotations

from rest_framework import serializers

from .models import Job


class JobSerializer(serializers.ModelSerializer):
    # Same trust model `apps.assets.models.Attachment` already uses for
    # `storage_key` (`AssetDetailScreen`/`AssetForm` build `/media/{storage_key}`
    # directly, no separate authenticated download endpoint) — a plain path
    # under the storage volume, served by nginx with `Content-Disposition:
    # attachment` (queued devops hardening, `apps.assets.services` module
    # docstring). `result_key` is namespaced with the job's own unguessable
    # UUID, same unguessable-path posture as an attachment's random filename
    # component. `null` until the job has actually succeeded.
    download_url = serializers.SerializerMethodField()

    class Meta:
        model = Job
        fields = [
            "id",
            "job_type",
            "status",
            "error",
            "result_filename",
            "download_url",
            "created_at",
            "updated_at",
            "started_at",
            "finished_at",
        ]
        read_only_fields = fields

    def get_download_url(self, obj: Job) -> str | None:
        if obj.status != Job.Status.SUCCEEDED or not obj.result_key:
            return None
        return f"/media/{obj.result_key}"
