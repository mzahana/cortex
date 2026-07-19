"""Tenant + the tenant-scoped base model (T0.4).

`Tenant` is the one table every other tenant-owned table points at.
`TenantScopedModel` is the abstract base every future tenant-owned model
(Asset, Category, Location, Project, Role, Membership, ...) must inherit —
this is what makes tenant isolation centralized rather than per-view
(docs/architecture.md §3, CLAUDE.md invariants).
"""

from __future__ import annotations

from django.db import models

from .managers import TenantScopedManager


class Tenant(models.Model):
    """A lab/organization. Not itself tenant-scoped (there is nothing "above"
    it) — the root of the multi-tenant tree."""

    name = models.CharField(max_length=255)
    slug = models.SlugField(max_length=100, unique=True)
    settings = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "tenancy_tenant"
        ordering = ["name"]

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.name


class TenantScopedModel(models.Model):
    """Abstract base for every tenant-owned table.

    - `tenant` is a required FK (never nullable) so RLS (T0.5) never needs a
      null-tenant carve-out.
    - `objects` (the default manager) is `TenantScopedManager`: every access
      is filtered by the tenant in the current request/task context, and
      raises `TenantContextError` if that context is unset (fail-closed).
    - `all_objects` is the plain, unscoped manager. It exists for the narrow,
      explicit cases where cross-tenant access is legitimate (seed data,
      management commands, the auth lookup path before a session exists).
      Reaching for `all_objects` should always be a deliberate, reviewable
      choice — never the accidental default.
    """

    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="+", db_index=True)

    objects = TenantScopedManager()
    all_objects = models.Manager()

    class Meta:
        abstract = True
