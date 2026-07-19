"""Custom `User` model (T0.4) — set as `AUTH_USER_MODEL` before any migration
exists, per the task's explicit instruction.

`User` is tenant-owned (`docs/data-model.md`: "email unique per tenant"), but
it is also Django's auth anchor, which creates one narrow, well-known
exception to "always query through the tenant-scoped manager": authentication
itself happens *before* any tenant context exists. That's handled by making
the *default* manager (`.objects`) the fail-closed `TenantScopedManager` as
usual, while wiring `Meta.default_manager_name`/`base_manager_name` to the
unscoped `all_objects` so Django's internals (authenticate(), createsuperuser)
use the unscoped path deliberately and explicitly — see `managers.py`.
"""

from __future__ import annotations

from django.contrib.auth.base_user import AbstractBaseUser
from django.db import models

from apps.tenancy.managers import TenantScopedManager
from apps.tenancy.models import Tenant

from .managers import UserManager


class User(AbstractBaseUser):
    tenant = models.ForeignKey(
        Tenant, on_delete=models.CASCADE, related_name="users", db_index=True
    )
    email = models.EmailField()
    name = models.CharField(max_length=255, blank=True)

    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)
    is_superuser = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = ["tenant"]

    # Ordinary app code doing `User.objects...` gets the tenant-scoped,
    # fail-closed manager (consistent with every other tenant-owned model).
    objects = TenantScopedManager()
    # Deliberately unscoped — auth internals only (see managers.py docstring).
    all_objects = UserManager()

    class Meta:
        db_table = "accounts_user"
        # Django's internal auth machinery (authenticate(), createsuperuser,
        # get_by_natural_key) must use the unscoped manager: there is no
        # tenant context yet at login time.
        default_manager_name = "all_objects"
        base_manager_name = "all_objects"
        constraints = [
            models.UniqueConstraint(fields=["tenant", "email"], name="uniq_user_tenant_email")
        ]
        indexes = [models.Index(fields=["tenant", "email"])]

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"{self.email} ({self.tenant_id})"

    def get_full_name(self) -> str:
        return self.name or self.email

    def get_short_name(self) -> str:
        return self.name or self.email

    # Minimal admin-site compatibility. This is NOT the application RBAC
    # system (see apps.rbac.services) — it only gates Django's own /admin.
    def has_perm(self, perm, obj=None) -> bool:
        return self.is_superuser

    def has_module_perms(self, app_label) -> bool:
        return self.is_superuser
