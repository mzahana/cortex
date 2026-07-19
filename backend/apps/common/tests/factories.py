"""Multi-tenant `factory_boy` factories (T0.9).

Kept in `apps.common.tests` (not per-app) so M1+ asset/category/location
factories can grow alongside these without a later re-shuffle, and so every
app's tests import multi-tenant fixtures from one reviewed place.

**Design principle — distinct tenants by default (explicit T0.9 requirement).**
Every factory that needs a tenant gets one via
`factory.SubFactory(TenantFactory)`, which creates a BRAND NEW `Tenant` on
every call unless a caller overrides it. That means two independently
constructed objects — e.g. `UserFactory()` and `UserFactory()` with no
`tenant=` kwarg — land in **two different tenants** by construction. A test
that naively wires up "two users" or "two projects" without thinking about
tenants gets cross-tenant data automatically, which is exactly what makes an
isolation bug (a forgotten `.filter(tenant=...)`, a manager that silently
falls back to unscoped) surface as a failing test instead of an accidental
pass. Tests that WANT same-tenant objects must say so explicitly:
`UserFactory(tenant=t)`, `ProjectFactory(tenant=t)`, `MembershipFactory(user=u)`.

**Why some factories override `_create`.** `TenantScopedModel.objects` (the
model's plain, unqualified default manager for most tenant-owned models) is
`TenantScopedManager`, which raises `TenantContextError` fail-closed with no
active `tenant_context()` (T0.4) — and factories run outside of any request
or explicit `tenant_context()` block. Rather than wrapping every factory
call in `with tenant_context(...):` (which would also push the RLS GUC and
complicate ordinary `db`-fixture tests that don't need it), factories for
`TenantScopedModel` subclasses create through `.all_objects` explicitly —
same deliberate, reviewed escape hatch the seed command
(`apps.accounts.management.commands.seed_t0_6`) already uses. `User` needs no
override: its `Meta.default_manager_name` is already `all_objects` (T0.4).
"""

from __future__ import annotations

import factory
import factory.django

from apps.accounts.models import User
from apps.projects.models import Project
from apps.rbac.models import Membership, Role
from apps.rbac.permission_keys import ROLE_MEMBER
from apps.tenancy.models import Tenant

DEFAULT_TEST_PASSWORD = "TestPass123!"


class TenantFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Tenant

    name = factory.Sequence(lambda n: f"Test Tenant {n}")
    slug = factory.Sequence(lambda n: f"test-tenant-{n}")


class UserFactory(factory.django.DjangoModelFactory):
    """Creates through `User.all_objects` explicitly.

    NOTE: `factory_boy`'s own manager resolution (`DjangoModelFactory.
    _get_manager`) always tries `model_class.objects` FIRST, regardless of
    `Meta.default_manager_name`/`base_manager_name` — it does not know about
    Django's manager-name indirection. `User.objects` is deliberately the
    fail-closed `TenantScopedManager` (see `apps.accounts.models.User`), so
    without this override every `UserFactory()` call would raise
    `TenantContextError` outside of a `tenant_context()` block. `.all_objects`
    is the same deliberate, reviewed escape hatch `LoginView`/`seed_t0_6` use.
    """

    class Meta:
        model = User
        skip_postgeneration_save = True

    tenant = factory.SubFactory(TenantFactory)
    email = factory.Sequence(lambda n: f"user{n}@example.test")
    name = factory.Faker("name")
    is_active = True

    @classmethod
    def _create(cls, model_class, *args, **kwargs):
        return model_class.all_objects.create(*args, **kwargs)

    @factory.post_generation
    def password(self, create, extracted, **kwargs):
        if not create:
            return
        raw = extracted or DEFAULT_TEST_PASSWORD
        self.set_password(raw)
        self.save(update_fields=["password"])


class ProjectFactory(factory.django.DjangoModelFactory):
    """`Project` is a `TenantScopedModel`; see module docstring for why this
    creates through `.all_objects` rather than the (context-requiring)
    default manager.
    """

    class Meta:
        model = Project

    tenant = factory.SubFactory(TenantFactory)
    name = factory.Sequence(lambda n: f"Test Project {n}")
    is_active = True

    @classmethod
    def _create(cls, model_class, *args, **kwargs):
        return model_class.all_objects.create(*args, **kwargs)


def get_role(tenant: Tenant, role_key: str) -> Role:
    """Fetch one of the 4 system roles auto-seeded for `tenant` the moment it
    was created (`apps.rbac.signals.seed_system_roles`, T0.4). Deliberately
    never creates a `Role` directly here — the seed migration/signal is the
    single source of truth for role/permission grants (docs/rbac.md §2/§3);
    a factory that fabricated its own `Role` rows could drift from it.
    """
    return Role.all_objects.get(tenant=tenant, key=role_key)


class MembershipFactory(factory.django.DjangoModelFactory):
    """Defaults to a tenant-wide (`project=None`) Member membership for a
    freshly made `UserFactory()` user.

    **Read before use:** every `UserFactory()`-created user is ALREADY given
    an automatic tenant-wide Member membership by
    `apps.rbac.signals.assign_default_membership` the moment the `User` row
    is inserted. Calling `MembershipFactory(user=u, role_key=ROLE_MEMBER)`
    (the default) on that same user would collide with
    `uniq_membership_user_role_project`. Use this factory to add an
    ADDITIONAL, DIFFERENTLY-SCOPED membership (e.g. a project-scoped one via
    `project=...`), and `upgrade_tenant_wide_role()` below to change the
    existing default tenant-wide membership's role in place instead of
    creating a second one.
    """

    class Meta:
        model = Membership

    class Params:
        role_key = ROLE_MEMBER

    user = factory.SubFactory(UserFactory)
    tenant = factory.SelfAttribute("user.tenant")
    role = factory.LazyAttribute(lambda o: get_role(o.tenant, o.role_key))
    project = None

    @classmethod
    def _create(cls, model_class, *args, **kwargs):
        return model_class.all_objects.create(*args, **kwargs)


def upgrade_tenant_wide_role(user: User, role_key: str) -> Membership:
    """Change `user`'s existing auto-created tenant-wide Membership to
    `role_key`, in place — mirrors `seed_t0_6.Command._seed_tenant`'s
    pattern and avoids a `uniq_membership_user_role_project` collision that
    creating a second tenant-wide membership would hit.
    """
    membership = Membership.all_objects.get(user=user, project__isnull=True)
    role = get_role(user.tenant, role_key)
    if membership.role_id != role.id:
        membership.role = role
        membership.save(update_fields=["role"])
    return membership


def add_project_membership(user: User, project: Project, role_key: str) -> Membership:
    """Add an ADDITIONAL project-scoped Membership for `user` on `project`,
    on top of whatever tenant-wide membership they already hold — the
    "Member tenant-wide + ProjectLead on Project X" pattern from
    docs/rbac.md §1's own example and `seed_t0_6`.
    """
    role = get_role(user.tenant, role_key)
    return Membership.all_objects.create(tenant=user.tenant, user=user, role=role, project=project)
