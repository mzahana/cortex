"""T5.1 exit-criterion proof (`docs/tasks/M5-notifications-audit-dashboard.md`,
F9 acceptance in `docs/features.md`):

1. an emitted event enqueues a Celery task and returns immediately (never
   blocks the request) -- `test_enqueue_returns_immediately_without_running_the_task`.
2. a forced failure (mocked provider raising) retries per the retry/backoff
   policy and ends up logged `failed` in `EmailLog` after exhausting
   retries -- `test_forced_failure_retries_then_logs_failed`.
3. a disabled optional event (`NotificationPref.email_enabled=False`)
   results in nothing being sent -- `test_disabled_optional_event_sends_nothing`.

Plus: the default provider really is `ConsoleProvider` in dev/test
(`test_console_provider_is_the_default_and_a_send_succeeds`), and the
minimal `notification-prefs` endpoint is tenant/RBAC-scoped to the caller's
own rows (`TestNotificationPrefApi`).
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest
from django.db import transaction
from django.test import override_settings

from apps.common.tests.factories import DEFAULT_TEST_PASSWORD, TenantFactory, UserFactory
from apps.notifications.models import EmailLog, NotificationPref
from apps.notifications.providers import ConsoleProvider, EmailSendError, get_email_provider
from apps.notifications.services import enqueue_transactional_email
from apps.notifications.tasks import send_transactional_email
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


class TestEnqueueNeverBlocks:
    def test_enqueue_returns_immediately_without_running_the_task(
        self, django_capture_on_commit_callbacks
    ):
        """Proves the caller-facing function hands off to Celery and returns
        WITHOUT waiting on the actual send. `CELERY_TASK_ALWAYS_EAGER` is
        forced OFF for this one test (test settings otherwise default it ON,
        specifically so slow-work tests don't need a live broker/worker) so
        `.delay()` really is the non-blocking, fire-and-forget call it is in
        production -- `.delay()` itself is mocked (no real broker is running
        in this sandbox, same rationale `config/settings/test.py` documents
        for swapping the cache/session backend) so this isolates exactly one
        thing: `enqueue_transactional_email` hands off via `.delay()` rather
        than ever calling the task function synchronously in-process.

        `django_capture_on_commit_callbacks(execute=False)` (same fixture
        `apps.stock`/`apps.reservations` tests already use with
        `execute=True`) captures the `transaction.on_commit` callback
        without running it yet, so this can assert the enqueuing call
        already returned -- with the `EmailLog` row written and `.delay`
        NOT yet invoked -- before the commit callback runs at all.

        The explicit `transaction.atomic()` block reproduces what
        `DATABASES["default"]["ATOMIC_REQUESTS"] = True` already gives every
        real request (the whole view runs inside one transaction, so
        `on_commit` naturally defers until the response is being sent) --
        this test calls the service function directly, not through a view,
        so it wraps it in one explicitly to get the same semantics.
        """
        tenant = TenantFactory()
        user = UserFactory(tenant=tenant)

        with override_settings(CELERY_TASK_ALWAYS_EAGER=False):
            with patch.object(send_transactional_email, "delay") as mock_delay:
                with tenant_context(tenant.id):
                    with django_capture_on_commit_callbacks(execute=False) as callbacks:
                        with transaction.atomic():
                            log_id = enqueue_transactional_email(
                                tenant_id=tenant.id,
                                event_type="synthetic.test_event",
                                template_id="tmpl-1",
                                to=user.email,
                                params={"foo": "bar"},
                                user=user,
                                optional=False,
                            )

                    # The service call has already returned here -- the
                    # EmailLog row exists, but nothing has been handed to
                    # Celery yet (that only happens once the transaction
                    # commits, captured but not yet run above).
                    assert log_id is not None
                    log = EmailLog.all_objects.get(pk=log_id)
                    assert log.status == EmailLog.Status.QUEUED
                    mock_delay.assert_not_called()
                    assert len(callbacks) == 1

                    # Now run the captured on_commit callback (equivalent to
                    # the request transaction committing) and confirm it
                    # calls `.delay(...)` -- never the task body directly.
                    for callback in callbacks:
                        callback()

        mock_delay.assert_called_once()
        call_kwargs = mock_delay.call_args.kwargs
        assert call_kwargs["email_log_id"] == log.id
        assert call_kwargs["tenant_id"] == tenant.id
        assert call_kwargs["template_id"] == "tmpl-1"


class TestRetryThenFailed:
    def test_forced_failure_retries_then_logs_failed(self):
        """Drives the REAL task body through Celery's OWN eager retry
        recursion (`Task.apply()`, when a task raises `self.retry(...)`,
        re-invokes itself via `retval.sig.apply(retries=retries + 1)` --
        see `celery/app/task.py`), not a hand-rolled substitute loop.

        `CELERY_TASK_EAGER_PROPAGATES` is overridden to `False` for JUST
        this test (test settings otherwise force it `True`, so that a
        genuinely unhandled exception in a task fails the test loudly
        rather than being silently swallowed -- the right default
        everywhere else). It must be `False` here specifically because
        Celery's internal retry-recursion (above) always re-applies with
        `throw=None -> settings.task_eager_propagates` on every recursive
        step, regardless of what was passed to the outermost `.apply()`
        call -- with it left `True`, the SECOND simulated attempt's retry
        would surface as a real raised exception instead of continuing the
        loop, which is a Celery eager-mode quirk, not something this task's
        own retry/backoff logic controls.
        """
        tenant = TenantFactory()
        user = UserFactory(tenant=tenant)

        with tenant_context(tenant.id):
            log = EmailLog.all_objects.create(
                tenant=tenant,
                user=user,
                recipient=user.email,
                event_type="synthetic.test_event",
                provider="ConsoleProvider",
                status=EmailLog.Status.QUEUED,
            )

        call_count = {"n": 0}

        def _always_fails(self, *, template_id, to, params, tags=None):
            call_count["n"] += 1
            raise EmailSendError("simulated transient outage")

        max_retries = send_transactional_email.max_retries
        assert max_retries == 5

        with override_settings(CELERY_TASK_EAGER_PROPAGATES=False):
            with patch(
                "apps.notifications.providers.ConsoleProvider.send_transactional",
                new=_always_fails,
            ):
                result = send_transactional_email.apply(
                    kwargs=dict(
                        email_log_id=log.id,
                        tenant_id=tenant.id,
                        template_id="tmpl-1",
                        to=user.email,
                        params={},
                    ),
                )

        assert result.successful(), result.result  # the task itself did not raise
        log.refresh_from_db()
        assert log.status == EmailLog.Status.FAILED
        assert "simulated transient outage" in log.error
        # One provider call per attempt: the initial attempt (0) plus
        # `max_retries` retries = 6 calls total.
        assert call_count["n"] == max_retries + 1


class TestPrefsGateNothingSent:
    def test_disabled_optional_event_sends_nothing(self):
        tenant = TenantFactory()
        user = UserFactory(tenant=tenant)
        with tenant_context(tenant.id):
            NotificationPref.all_objects.create(
                tenant=tenant,
                user=user,
                event_type="synthetic.test_event",
                email_enabled=False,
            )

            result = enqueue_transactional_email(
                tenant_id=tenant.id,
                event_type="synthetic.test_event",
                template_id="tmpl-1",
                to=user.email,
                params={},
                user=user,
                optional=True,
            )

        assert result is None
        assert not EmailLog.all_objects.filter(
            tenant_id=tenant.id, user=user, event_type="synthetic.test_event"
        ).exists()

    def test_enabled_by_default_when_no_pref_row_exists(self, django_capture_on_commit_callbacks):
        """Opt-out, not opt-in: no explicit `NotificationPref` row -> the
        event still sends (matches every role holding `notify.self` by
        default, docs/rbac.md)."""
        tenant = TenantFactory()
        user = UserFactory(tenant=tenant)
        with tenant_context(tenant.id):
            with django_capture_on_commit_callbacks(execute=True):
                with transaction.atomic():
                    log_id = enqueue_transactional_email(
                        tenant_id=tenant.id,
                        event_type="synthetic.another_event",
                        template_id="tmpl-1",
                        to=user.email,
                        params={},
                        user=user,
                        optional=True,
                    )

        assert log_id is not None
        log = EmailLog.all_objects.get(pk=log_id)
        assert log.status == EmailLog.Status.SENT

    def test_non_optional_event_ignores_a_disabled_pref(self, django_capture_on_commit_callbacks):
        """`optional=False` (a hypothetical security/transactional event) is
        never gated by `NotificationPref`."""
        tenant = TenantFactory()
        user = UserFactory(tenant=tenant)
        with tenant_context(tenant.id):
            NotificationPref.all_objects.create(
                tenant=tenant,
                user=user,
                event_type="synthetic.mandatory_event",
                email_enabled=False,
            )
            with django_capture_on_commit_callbacks(execute=True):
                with transaction.atomic():
                    log_id = enqueue_transactional_email(
                        tenant_id=tenant.id,
                        event_type="synthetic.mandatory_event",
                        template_id="tmpl-1",
                        to=user.email,
                        params={},
                        user=user,
                        optional=False,
                    )

        assert log_id is not None
        log = EmailLog.all_objects.get(pk=log_id)
        assert log.status == EmailLog.Status.SENT


def test_console_provider_is_the_default_and_a_send_succeeds(settings):
    assert settings.NOTIFICATION_EMAIL_PROVIDER == "apps.notifications.providers.ConsoleProvider"
    provider = get_email_provider()
    assert isinstance(provider, ConsoleProvider)
    result = provider.send_transactional(template_id="tmpl-1", to="a@example.test", params={})
    assert result.provider_message_id.startswith("console-")


class TestNotificationPrefApi:
    def test_list_and_patch_own_prefs_only(self, client):
        tenant = TenantFactory()
        user = UserFactory(tenant=tenant)
        other = UserFactory(tenant=tenant)
        with tenant_context(tenant.id):
            NotificationPref.all_objects.create(
                tenant=tenant, user=other, event_type="synthetic.test_event", email_enabled=False
            )

        _login(client, tenant, user)

        response = client.get("/api/v1/notification-prefs/")
        assert response.status_code == 200, response.content
        assert response.json()["results"] == []  # `other`'s row must never appear

        response = client.patch(
            "/api/v1/notification-prefs/synthetic.test_event/",
            data=json.dumps({"email_enabled": False}),
            content_type="application/json",
        )
        assert response.status_code == 200, response.content
        assert response.json()["email_enabled"] is False

        with tenant_context(tenant.id):
            own_pref = NotificationPref.all_objects.get(
                user=user, event_type="synthetic.test_event"
            )
            assert own_pref.email_enabled is False
            # `other`'s pref for the same event_type is untouched.
            other_pref = NotificationPref.all_objects.get(
                user=other, event_type="synthetic.test_event"
            )
            assert other_pref.email_enabled is False  # unchanged (was already False)

    def test_cross_tenant_user_cannot_see_or_touch_another_tenants_prefs(self, client):
        tenant_a = TenantFactory()
        tenant_b = TenantFactory()
        user_a = UserFactory(tenant=tenant_a)
        user_b = UserFactory(tenant=tenant_b)
        with tenant_context(tenant_b.id):
            NotificationPref.all_objects.create(
                tenant=tenant_b, user=user_b, event_type="synthetic.test_event", email_enabled=False
            )

        _login(client, tenant_a, user_a)
        response = client.get("/api/v1/notification-prefs/")
        assert response.status_code == 200, response.content
        assert response.json()["results"] == []

    def test_requires_authentication(self, client):
        response = client.get("/api/v1/notification-prefs/")
        assert response.status_code in (401, 403)
