"""T4.5 — `GET /api/v1/jobs/{id}`: polling shape, creator-only read scope,
tenant isolation. Label-specific job CONTENT (asset ids/template/PDF) is
covered end-to-end in `apps/labels/tests/`; this module only exercises the
generic `Job`/`JobRetrieveView` contract, independent of `label_pdf`.
"""

from __future__ import annotations

import pytest

from apps.common.tests.factories import DEFAULT_TEST_PASSWORD, TenantFactory, UserFactory
from apps.jobs.models import Job
from apps.tenancy.context import tenant_context

pytestmark = pytest.mark.django_db


def _login(client, tenant, user):
    response = client.post(
        "/api/v1/auth/login",
        {"tenant": tenant.slug, "email": user.email, "password": DEFAULT_TEST_PASSWORD},
        content_type="application/json",
    )
    assert response.status_code == 200, response.content
    return response


def _make_job(tenant, user, **kwargs) -> Job:
    with tenant_context(tenant.id):
        defaults = {
            "tenant": tenant,
            "job_type": "label_pdf",
            "created_by": user,
        }
        defaults.update(kwargs)
        return Job.all_objects.create(**defaults)


class TestJobRetrieve:
    def test_creator_can_poll_own_queued_job(self, client):
        tenant = TenantFactory()
        user = UserFactory(tenant=tenant)
        job = _make_job(tenant, user)
        _login(client, tenant, user)

        response = client.get(f"/api/v1/jobs/{job.id}")
        assert response.status_code == 200, response.content
        body = response.json()
        assert body["id"] == str(job.id)
        assert body["status"] == "queued"
        assert body["download_url"] is None

    def test_succeeded_job_exposes_download_url(self, client):
        tenant = TenantFactory()
        user = UserFactory(tenant=tenant)
        job = _make_job(
            tenant,
            user,
            status=Job.Status.SUCCEEDED,
            result_key="labels/1/abc.pdf",
            result_filename="labels-abc.pdf",
            result_content_type="application/pdf",
        )
        _login(client, tenant, user)

        response = client.get(f"/api/v1/jobs/{job.id}")
        assert response.status_code == 200, response.content
        body = response.json()
        assert body["status"] == "succeeded"
        assert body["download_url"] == "/media/labels/1/abc.pdf"
        assert body["result_filename"] == "labels-abc.pdf"

    def test_failed_job_exposes_error(self, client):
        tenant = TenantFactory()
        user = UserFactory(tenant=tenant)
        job = _make_job(tenant, user, status=Job.Status.FAILED, error="boom")
        _login(client, tenant, user)

        response = client.get(f"/api/v1/jobs/{job.id}")
        assert response.status_code == 200, response.content
        body = response.json()
        assert body["status"] == "failed"
        assert body["error"] == "boom"
        assert body["download_url"] is None

    def test_another_users_job_404s_even_same_tenant(self, client):
        """Deliberately narrow RBAC (task instructions/module docstring):
        even an Admin in the SAME tenant cannot poll someone else's job —
        a job is a private polling handle, not a shared resource."""
        tenant = TenantFactory()
        owner = UserFactory(tenant=tenant)
        other = UserFactory(tenant=tenant)
        job = _make_job(tenant, owner)
        _login(client, tenant, other)

        response = client.get(f"/api/v1/jobs/{job.id}")
        assert response.status_code == 404

    def test_cross_tenant_job_id_404s(self, client):
        tenant_a = TenantFactory()
        tenant_b = TenantFactory()
        owner = UserFactory(tenant=tenant_a)
        job = _make_job(tenant_a, owner)

        outsider = UserFactory(tenant=tenant_b)
        _login(client, tenant_b, outsider)

        response = client.get(f"/api/v1/jobs/{job.id}")
        assert response.status_code == 404

    def test_unauthenticated_request_is_denied(self, client):
        tenant = TenantFactory()
        user = UserFactory(tenant=tenant)
        job = _make_job(tenant, user)

        response = client.get(f"/api/v1/jobs/{job.id}")
        assert response.status_code in (401, 403)

    def test_unknown_job_id_404s(self, client):
        tenant = TenantFactory()
        user = UserFactory(tenant=tenant)
        _login(client, tenant, user)

        response = client.get("/api/v1/jobs/00000000-0000-0000-0000-000000000000")
        assert response.status_code == 404
