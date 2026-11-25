"""Bulk conversion of existing assets to `tracking=counted`.

Backs the `convert_to_counted` management command. Every function here must
run inside `tenant_context(...)` (tenant-scoped managers + RLS); nothing needs
an owner/superuser connection.

The per-row rules are NOT re-implemented: `plan_row` calls the API
serializer's `_validate_tracking` (which itself delegates the blocker checks to
`apps.assets.services.counted_blockers`), so the command and
`PATCH /api/v1/assets/{id}` can never disagree.

Money safety: `financial_fingerprint` captures every figure the projects API
computes (`budget_rollup`, `resolve_asset_allocations`) plus the raw expense
and `ExpenseAssetLink` rows. `apply_conversions` takes it before and after the
writes inside one transaction and aborts (rolling back) on any difference.
"""

from __future__ import annotations

import re
import uuid as uuid_lib
from dataclasses import dataclass, field
from typing import Any

from django.db import transaction
from rest_framework import serializers

from apps.audit.services import write_audit_log
from apps.projects.models import Expense, ExpenseAssetLink, Project
from apps.projects.services import budget_rollup, resolve_asset_allocations
from apps.rbac.permission_keys import ASSET_EDIT

from .models import Asset
from .serializers import AssetSerializer
from .services import asset_tracking_snapshot


class FinancialGuardError(Exception):
    """Financial figures differed before vs after the conversion."""

    def __init__(self, diff: list[str]):
        super().__init__("financial figures changed during conversion")
        self.diff = diff


@dataclass
class RowPlan:
    line: int
    raw_id: str
    asset: Asset | None = None
    quantity: int | None = None
    error: str | None = None
    note: str = ""
    noop: bool = False
    before: dict[str, Any] | None = None
    after: dict[str, Any] | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


@dataclass
class Plan:
    rows: list[RowPlan] = field(default_factory=list)
    skipped: int = 0

    @property
    def to_convert(self) -> list[RowPlan]:
        return [r for r in self.rows if r.ok and not r.noop]

    @property
    def errors(self) -> list[RowPlan]:
        return [r for r in self.rows if not r.ok]


# --- export ------------------------------------------------------------------


def candidate_assets() -> list[Asset]:
    return list(
        Asset.objects.filter(is_consumable=False, tracking=Asset.Tracking.INDIVIDUAL)
        .exclude(status=Asset.Status.RETIRED)
        .select_related("category", "project", "location")
        .order_by("id")
    )


def duplicate_groups(assets: list[Asset]) -> dict[int, str]:
    """`{asset.id: group label}` for candidates sharing (lower(trim(name)),
    category) with at least one other candidate. Informational only."""
    buckets: dict[tuple[str, int], list[Asset]] = {}
    for a in assets:
        buckets.setdefault((a.name.strip().lower(), a.category_id), []).append(a)
    out: dict[int, str] = {}
    n = 0
    for members in buckets.values():
        if len(members) < 2:
            continue
        n += 1
        label = f"G{n}"
        for a in members:
            out[a.id] = label
    return out


# --- validate ----------------------------------------------------------------


def _error_text(exc: serializers.ValidationError) -> str:
    detail = exc.detail
    if isinstance(detail, dict):
        parts: list[str] = []
        for value in detail.values():
            items = value if isinstance(value, list) else [value]
            parts.extend(str(i) for i in items)
        return " ".join(parts)
    return " ".join(str(i) for i in (detail if isinstance(detail, list) else [detail]))


def validate_conversion(asset: Asset, quantity: int) -> None:
    """Raise `serializers.ValidationError` if the API would refuse
    `PATCH {tracking: counted, quantity: <quantity>}` for `asset`."""
    if asset.status == Asset.Status.RETIRED:
        raise serializers.ValidationError({"status": "Retired assets can't be converted."})
    AssetSerializer()._validate_tracking(
        {"tracking": Asset.Tracking.COUNTED, "quantity": quantity}, asset
    )


MAX_QUANTITY = 2_147_483_647  # Postgres integer max (Asset.quantity column)
_INT_RE = re.compile(r"\d+(\.0+)?", re.ASCII)  # Excel turns 18 into "18.0"


def parse_int(raw: str) -> int | None:
    """ASCII digits only (optionally Excel's trailing `.0`); never raises."""
    raw = raw.strip()
    if not _INT_RE.fullmatch(raw):
        return None
    return int(raw.split(".")[0])


def _lookup_id(raw: str) -> Asset | None:
    n = parse_int(raw)
    return Asset.objects.filter(pk=n).first() if n is not None else None


def _lookup_uuid(raw: str) -> Asset | None:
    try:
        return Asset.objects.filter(uuid=uuid_lib.UUID(raw.strip())).first()
    except ValueError:
        return None


def plan_rows(rows: list[dict[str, str]]) -> Plan:
    """Validate CSV rows (dicts with `id`/`uuid` + `quantity`, optional `name`
    and `_line`) into a `Plan`."""
    plan = Plan()
    seen: dict[int, RowPlan] = {}
    for i, row in enumerate(rows, start=2):  # line 1 is the header
        qty_raw = (row.get("quantity") or "").strip()
        if not qty_raw:
            plan.skipped += 1
            continue
        id_raw = (row.get("id") or "").strip()
        uuid_raw = (row.get("uuid") or "").strip()
        line = parse_int(row.get("_line", "") or "") or i
        rp = RowPlan(line=line, raw_id=id_raw or uuid_raw)
        plan.rows.append(rp)
        if not (id_raw or uuid_raw):
            rp.error = "missing id/uuid"
            continue
        qty = parse_int(qty_raw)
        if qty is None or qty < 1:
            rp.error = f"quantity must be a positive integer (got {qty_raw!r})"
            continue
        if qty > MAX_QUANTITY:
            rp.error = f"quantity too large (max {MAX_QUANTITY})"
            continue
        rp.quantity = qty
        by_id = _lookup_id(id_raw) if id_raw else None
        by_uuid = _lookup_uuid(uuid_raw) if uuid_raw else None
        if (id_raw and by_id is None) or (uuid_raw and by_uuid is None):
            rp.error = "unknown asset id/uuid for this tenant"
            continue
        if by_id is not None and by_uuid is not None and by_id.pk != by_uuid.pk:
            rp.error = "id and uuid don't match - was the sheet re-sorted?"
            continue
        asset = by_id or by_uuid
        assert asset is not None
        rp.asset = asset
        if "name" in row and row["name"].strip().casefold() != asset.name.strip().casefold():
            rp.error = (
                f"name {row['name']!r} doesn't match the asset's name {asset.name!r} "
                "- was the sheet re-sorted or the name edited?"
            )
            continue
        if asset.id in seen:
            first = seen[asset.id]
            rp.error = f"duplicate of line {first.line}"
            if first.ok:
                first.error = f"duplicate id (also on line {line})"
            continue
        seen[asset.id] = rp
        try:
            validate_conversion(asset, qty)
        except serializers.ValidationError as exc:
            rp.error = _error_text(exc)
            continue
        rp.before = asset_tracking_snapshot(asset)
        rp.after = {**rp.before, "tracking": Asset.Tracking.COUNTED, "quantity": qty}
        if asset.tracking == Asset.Tracking.COUNTED:
            if asset.quantity == qty:
                rp.noop = True
                rp.note = "unchanged"
            else:
                rp.note = "already counted, quantity update"
    return plan


# --- financial fingerprint ----------------------------------------------------


def financial_fingerprint() -> dict[str, Any]:
    """Every project financial figure the API computes, plus the underlying
    expense / asset-link rows, as plain comparable data (Decimals as str)."""
    projects: dict[str, Any] = {}
    for project in Project.objects.order_by("id"):
        roll = budget_rollup(project)
        projects[str(project.id)] = {
            "budget_total": str(roll["budget_total"]),
            "spent": str(roll["spent"]),
            "remaining": str(roll["remaining"]),
            "spend_by_category": [
                [r["category_id"], r["category"], str(r["total"])]
                for r in roll["spend_by_category"]
            ],
        }
    expenses: dict[str, Any] = {}
    for exp in Expense.objects.prefetch_related("asset_links").order_by("id"):
        expenses[str(exp.id)] = {
            "project": exp.project_id,
            "amount": str(exp.amount),
            "asset": exp.asset_id,
            "allocations": {
                str(k): str(v) for k, v in sorted(resolve_asset_allocations(exp).items())
            },
        }
    links = [
        [k.id, k.expense_id, k.asset_id, str(k.allocated_amount), k.quantity]
        for k in ExpenseAssetLink.objects.order_by("id")
    ]
    return {"projects": projects, "expenses": expenses, "links": links}


def fingerprint_counts(fp: dict[str, Any]) -> tuple[int, int]:
    return len(fp["projects"]), len(fp["expenses"])


def fingerprint_diff(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    diff: list[str] = []
    for section in ("projects", "expenses"):
        b, a = before[section], after[section]
        for key in sorted(set(b) | set(a), key=int):
            if b.get(key) != a.get(key):
                diff.append(f"{section[:-1]} {key}: {b.get(key)!r} -> {a.get(key)!r}")
    if before["links"] != after["links"]:
        diff.append(f"expense asset links: {before['links']!r} -> {after['links']!r}")
    return diff


# --- apply ---------------------------------------------------------------------


def apply_conversions(plan: Plan, *, tenant_id: int, actor: Any = None) -> tuple[int, int]:
    """Convert every row of a CLEAN `plan` in ONE transaction. Re-checks the
    guards on each locked row; raises (rolling back everything) on any
    failure, including a changed financial fingerprint. Returns the
    (projects, expenses) counts of the verified fingerprint.
    """
    assert not plan.errors
    with transaction.atomic():
        before_fp = financial_fingerprint()
        for rp in plan.rows:
            assert rp.asset is not None and rp.quantity is not None
            if rp.noop:
                continue
            locked = Asset.objects.select_for_update().get(pk=rp.asset.pk)
            validate_conversion(locked, rp.quantity)  # raises ValidationError
            before = asset_tracking_snapshot(locked)
            locked.tracking = Asset.Tracking.COUNTED
            locked.quantity = rp.quantity
            locked.save()
            write_audit_log(
                tenant_id=tenant_id,
                actor=actor,
                action=ASSET_EDIT,
                entity_type="asset",
                entity_id=locked.id,
                before=before,
                after=asset_tracking_snapshot(locked),
                ip=None,
            )
        after_fp = financial_fingerprint()
        if before_fp != after_fp:
            raise FinancialGuardError(fingerprint_diff(before_fp, after_fp))
    return fingerprint_counts(after_fp)
