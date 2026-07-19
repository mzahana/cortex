"""Exit-criterion verification for T0.4 (run manually / in CI, not a test).

Demonstrates, against the real DB:
  1. The union-of-memberships resolver returns the correct permission set for
     each of the 4 system roles, including the project-scoped
     (ProjectLead limited to their project) vs tenant-wide (Admin) distinction.
  2. A newly created user gets a tenant-wide Member membership by default.
  3. The tenant-scoped base manager fails closed with no tenant in context,
     and returns only the current tenant's rows once it's set — including
     across two different tenants, proving no cross-tenant leakage.

Usage: `python manage.py verify_t0_4`
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
from apps.rbac.services import get_effective_permissions
from apps.tenancy.context import TenantContextError, tenant_context
from apps.tenancy.models import Tenant


class Command(BaseCommand):
    help = "Verify the T0.4 exit criterion end-to-end (resolver + fail-closed manager)."

    def handle(self, *args, **options):
        self.stdout.write(self.style.MIGRATE_HEADING("=== T0.4 verification ==="))

        tenant_a, _ = Tenant.objects.get_or_create(
            slug="acme-robotics", defaults={"name": "Acme Robotics Lab"}
        )
        tenant_b, _ = Tenant.objects.get_or_create(slug="other-lab", defaults={"name": "Other Lab"})

        with tenant_context(tenant_a.id):
            project_x, _ = Project.all_objects.get_or_create(tenant=tenant_a, name="Project X")
            project_y, _ = Project.all_objects.get_or_create(
                tenant=tenant_a, name="Project Y (not Alice's)"
            )

            admin_user, _ = User.all_objects.get_or_create(
                tenant=tenant_a,
                email="admin@acme.test",
                defaults={"name": "Admin Ada"},
            )
            lead_user, _ = User.all_objects.get_or_create(
                tenant=tenant_a,
                email="lead@acme.test",
                defaults={"name": "Lead Leo"},
            )
            member_user, _ = User.all_objects.get_or_create(
                tenant=tenant_a,
                email="member@acme.test",
                defaults={"name": "Member Mia"},
            )
            viewer_user, _ = User.all_objects.get_or_create(
                tenant=tenant_a,
                email="viewer@acme.test",
                defaults={"name": "Viewer Vic"},
            )

            roles = {r.key: r for r in Role.all_objects.filter(tenant=tenant_a)}

            # Every user above just got a tenant-wide Member membership for
            # free, via the `post_save(User)` signal (apps/rbac/signals.py) —
            # this IS the "new users default to Member" exit criterion.
            # Verify that here, BEFORE elevating anyone, so the assertion is
            # about what the signal actually did, not what this script did.
            self.stdout.write(
                self.style.MIGRATE_HEADING("\n--- (1) New users default to Member ---")
            )
            for u in (admin_user, lead_user, member_user, viewer_user):
                default_membership = (
                    Membership.objects.filter(user=u, project__isnull=True)
                    .select_related("role")
                    .get()
                )
                ok = default_membership.role.key == ROLE_MEMBER
                self.stdout.write(
                    f"{u.email}: auto-created tenant-wide membership role = "
                    f"{default_membership.role.key!r} "
                    f"(expected {ROLE_MEMBER!r}): {'PASS' if ok else 'FAIL'}"
                )
                assert ok, f"{u.email} did not default to Member"

            # Now elevate admin_user/viewer_user by UPGRADING their default
            # membership's role (an admin action in the real product would
            # do the same thing via `role.assign`) rather than stacking a
            # second tenant-wide membership.
            admin_membership = Membership.objects.get(user=admin_user, project__isnull=True)
            admin_membership.role = roles[ROLE_ADMIN]
            admin_membership.save(update_fields=["role"])

            viewer_membership = Membership.objects.get(user=viewer_user, project__isnull=True)
            viewer_membership.role = roles[ROLE_VIEWER]
            viewer_membership.save(update_fields=["role"])

            # lead_user keeps their default tenant-wide Member membership
            # AND additionally gets a Project-Lead membership scoped to
            # Project X ONLY — exactly the docs/rbac.md §1 example ("Member
            # tenant-wide + ProjectLead on Project X").
            Membership.all_objects.get_or_create(
                tenant=tenant_a,
                user=lead_user,
                role=roles[ROLE_PROJECT_LEAD],
                project=project_x,
            )

            self.stdout.write(
                self.style.MIGRATE_HEADING("\n--- (2) Effective permissions per role ---")
            )
            for label, user, project in [
                ("Admin, general pool (project=None)", admin_user, None),
                ("Admin, Project X", admin_user, project_x),
                ("ProjectLead, Project X (their project)", lead_user, project_x),
                ("ProjectLead, Project Y (NOT their project)", lead_user, project_y),
                ("ProjectLead, general pool (project=None)", lead_user, None),
                ("Member, general pool", member_user, None),
                ("Member, Project X", member_user, project_x),
                ("Viewer, general pool", viewer_user, None),
                ("Viewer, Project X", viewer_user, project_x),
            ]:
                perms = sorted(get_effective_permissions(user, project=project))
                self.stdout.write(f"{label}:\n  {perms}")

            # Sanity assertions matching docs/rbac.md §1 scope rule exactly.
            admin_general = get_effective_permissions(admin_user, project=None)
            lead_on_their_project = get_effective_permissions(lead_user, project=project_x)
            lead_off_project = get_effective_permissions(lead_user, project=project_y)
            lead_general_pool = get_effective_permissions(lead_user, project=None)

            assert (
                "asset.create" in admin_general
            ), "Admin must be able to create assets tenant-wide"
            assert "tenant.manage" in admin_general, "Admin must hold tenant.manage"
            assert (
                "asset.create" in lead_on_their_project
            ), "ProjectLead must manage their own project"
            assert (
                "asset.create" not in lead_off_project
            ), "ProjectLead's power must NOT extend to a project they don't lead"
            assert "asset.create" not in lead_general_pool, (
                "ProjectLead's power must NOT extend to the general pool "
                "(rbac.md §1: general-pool assets are governed by tenant-wide "
                "permissions only)"
            )
            member_general = get_effective_permissions(member_user, project=None)
            assert "asset.create" not in member_general, "Member must not create assets"
            assert "reservation.create" in member_general, "Member must be able to reserve"
            viewer_general = get_effective_permissions(viewer_user, project=None)
            assert viewer_general == {
                "asset.view",
                "notify.self",
            }, f"Viewer must be exactly read-only + notify.self, got {viewer_general}"
            self.stdout.write(self.style.SUCCESS("All resolver assertions PASSED."))

        self.stdout.write(
            self.style.MIGRATE_HEADING("\n--- (3) Fail-closed tenant-scoped manager ---")
        )
        try:
            list(Membership.objects.all())
            self.stdout.write(self.style.ERROR("FAIL: query succeeded with NO tenant in context!"))
        except TenantContextError as exc:
            self.stdout.write(
                self.style.SUCCESS(f"PASS: raised TenantContextError as expected: {exc}")
            )

        with tenant_context(tenant_a.id):
            count_a = Membership.objects.count()
            emails_a = sorted(u.email for u in User.objects.all())
        with tenant_context(tenant_b.id):
            count_b = Membership.objects.count()
            emails_b = sorted(u.email for u in User.objects.all())

        self.stdout.write(f"tenant_a memberships visible: {count_a}, users: {emails_a}")
        self.stdout.write(f"tenant_b memberships visible: {count_b}, users: {emails_b}")
        assert count_a > 0, "tenant_a should see its own memberships"
        assert all(e.endswith("@acme.test") for e in emails_a), "tenant_a leaked a non-acme user"
        assert not emails_b, "tenant_b must NOT see tenant_a's users (cross-tenant leak, R4)"
        self.stdout.write(
            self.style.SUCCESS(
                "PASS: each tenant context sees only its own rows; no cross-tenant leakage."
            )
        )

        self.stdout.write(
            self.style.SUCCESS("\n=== T0.4 verification complete: ALL CHECKS PASSED ===")
        )
