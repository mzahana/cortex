"""Idempotent T0.6 auth-demo seed.

Creates 2 tenants, each with an Admin/ProjectLead/Member/Viewer user (a known
dev-only password so the exit-criterion login/`/me`/lockout checks can be run
by hand against a real `docker compose` stack), **plus** one email
(`shared@cross-tenant.test`) that exists in BOTH tenants with different roles
— the fixture the cross-tenant login-safety check needs (docs/tasks/M0-
foundations.md T0.6 exit criterion): tenant A's slug must authenticate only
tenant A's user for that email, never tenant B's.

Safe to re-run (`get_or_create` + always reasserting the dev password/role),
matching the pattern in `apps.rbac.management.commands.verify_t0_4`.

Usage: `python manage.py seed_t0_6`
"""

from __future__ import annotations

from django.core.management.base import BaseCommand

from apps.accounts.models import User
from apps.projects.models import Project
from apps.rbac.models import Membership, Role
from apps.rbac.permission_keys import (
    ROLE_ADMIN,
    ROLE_MEMBER,
    ROLE_PROJECT_LEAD,
    ROLE_VIEWER,
)
from apps.tenancy.context import tenant_context
from apps.tenancy.models import Tenant

# Dev-only credential, never used outside a local/CI throwaway stack.
DEV_PASSWORD = "DevPass123!"
SHARED_EMAIL = "shared@cross-tenant.test"

_ROLE_LOCALPARTS = {
    ROLE_ADMIN: "admin",
    ROLE_PROJECT_LEAD: "lead",
    ROLE_MEMBER: "member",
    ROLE_VIEWER: "viewer",
}


class Command(BaseCommand):
    help = "Idempotent T0.6 seed: 2 tenants x 4 roles + a cross-tenant shared-email fixture."

    def handle(self, *args, **options):
        tenant_a, _ = Tenant.objects.get_or_create(
            slug="acme-robotics", defaults={"name": "Acme Robotics Lab"}
        )
        tenant_b, _ = Tenant.objects.get_or_create(slug="other-lab", defaults={"name": "Other Lab"})

        self._seed_tenant(tenant_a, label="acme", shared_role=ROLE_ADMIN)
        self._seed_tenant(tenant_b, label="other", shared_role=ROLE_VIEWER)

        self.stdout.write(self.style.SUCCESS("\n=== T0.6 seed complete ==="))
        self.stdout.write(
            "Login payload shape: "
            '{"tenant": "acme-robotics" | "other-lab", "email": "<role>@<acme|other>.test", '
            f'"password": "{DEV_PASSWORD}"}}'
        )
        self.stdout.write(
            f"Cross-tenant probe: {SHARED_EMAIL!r} exists in BOTH tenants "
            f"(role={ROLE_ADMIN!r} in acme-robotics, role={ROLE_VIEWER!r} in "
            f"other-lab), same password — logging in with tenant=acme-robotics "
            f"must authenticate ONLY the acme-robotics user (Admin perms), "
            f"never the other-lab one."
        )

    def _seed_tenant(self, tenant: Tenant, *, label: str, shared_role: str) -> None:
        with tenant_context(tenant.id):
            project, _ = Project.all_objects.get_or_create(
                tenant=tenant, name=f"Project {label.title()} Alpha"
            )
            roles = {r.key: r for r in Role.all_objects.filter(tenant=tenant)}

            users: dict[str, User] = {}
            for role_key, localpart in _ROLE_LOCALPARTS.items():
                email = f"{localpart}@{label}.test"
                user, _ = User.all_objects.get_or_create(
                    tenant=tenant,
                    email=email,
                    defaults={"name": f"{role_key.replace('_', ' ').title()} ({label})"},
                )
                user.set_password(DEV_PASSWORD)
                user.save(update_fields=["password"])
                users[role_key] = user

            # Every user above already got a tenant-wide Member membership
            # for free from the `post_save(User)` signal
            # (apps/rbac/signals.py). Upgrade Admin/Viewer's default
            # membership in place; leave Member as-is; give ProjectLead an
            # ADDITIONAL project-scoped membership on top of their default
            # tenant-wide Member one -- exactly docs/rbac.md §1's own example
            # ("Member tenant-wide + ProjectLead on Project X"), so the seed
            # demonstrates the general-pool-vs-project-scope distinction
            # rather than accidentally granting ProjectLead tenant-wide power.
            for role_key in (ROLE_ADMIN, ROLE_VIEWER):
                membership = Membership.objects.get(user=users[role_key], project__isnull=True)
                if membership.role_id != roles[role_key].id:
                    membership.role = roles[role_key]
                    membership.save(update_fields=["role"])

            Membership.all_objects.get_or_create(
                tenant=tenant,
                user=users[ROLE_PROJECT_LEAD],
                role=roles[ROLE_PROJECT_LEAD],
                project=project,
            )

            # The cross-tenant fixture: same email, different tenant, given a
            # DIFFERENT role per tenant so a leak is immediately obvious in
            # `/me`'s permission set (Admin's full matrix vs Viewer's
            # read-only one) rather than only in the user's `id`/`name`.
            shared_user, _ = User.all_objects.get_or_create(
                tenant=tenant,
                email=SHARED_EMAIL,
                defaults={"name": f"Shared Probe ({label})"},
            )
            shared_user.set_password(DEV_PASSWORD)
            shared_user.save(update_fields=["password"])
            shared_membership = Membership.objects.get(user=shared_user, project__isnull=True)
            if shared_membership.role_id != roles[shared_role].id:
                shared_membership.role = roles[shared_role]
                shared_membership.save(update_fields=["role"])
