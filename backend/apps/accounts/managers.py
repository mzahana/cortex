"""`User.all_objects` — the deliberately unscoped manager (T0.4).

Django's auth machinery (`authenticate()`, `createsuperuser`, password reset,
etc.) needs to look a user up **before** any tenant context exists (there is
no session yet at login time). That lookup is inherently cross-tenant at the
SQL level (email is only unique *per tenant*, per docs/data-model.md), so it
cannot go through the fail-closed `TenantScopedManager`.

This manager is wired as `Meta.default_manager_name` on `User` (see
`models.py`) so Django's internals use it automatically, while ordinary
application code doing `User.objects.filter(...)` still gets the tenant-scoped,
fail-closed manager by default. Reaching for `User.all_objects` directly in
app/view code should be rare and reviewable (e.g. the login view in T0.6,
which is expected to resolve `(tenant, email)` itself before a session
exists).
"""

from __future__ import annotations

from django.contrib.auth.base_user import BaseUserManager


class UserManager(BaseUserManager):
    use_in_migrations = True

    def _create_user(self, tenant, email, password, **extra_fields):
        if not tenant:
            raise ValueError("Users must belong to a tenant")
        if not email:
            raise ValueError("Users must have an email address")
        email = self.normalize_email(email)
        user = self.model(tenant=tenant, email=email, **extra_fields)
        user.set_password(password)  # type: ignore[attr-defined]
        user.save(using=self._db)
        return user

    def create_user(self, tenant, email, password=None, **extra_fields):
        extra_fields.setdefault("is_staff", False)
        extra_fields.setdefault("is_superuser", False)
        return self._create_user(tenant, email, password, **extra_fields)

    def create_superuser(self, tenant, email, password=None, **extra_fields):
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)
        if extra_fields.get("is_staff") is not True:
            raise ValueError("Superuser must have is_staff=True")
        if extra_fields.get("is_superuser") is not True:
            raise ValueError("Superuser must have is_superuser=True")
        return self._create_user(tenant, email, password, **extra_fields)
