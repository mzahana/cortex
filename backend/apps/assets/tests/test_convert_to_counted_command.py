"""`manage.py convert_to_counted` — export / dry-run / apply, tenant isolation,
the all-or-nothing rule, the financial-figures guard, and a run over the real
RLS-subject `cortex_app` connection."""

from __future__ import annotations

import csv
import io
import sys
from datetime import timedelta
from decimal import Decimal

import pytest
from django.core.management import call_command
from django.utils import timezone

from apps.accounts.models import User
from apps.assets import conversion
from apps.assets.models import Asset
from apps.audit.models import AuditLog
from apps.common.tests.factories import (
    AssetFactory,
    CategoryFactory,
    ExpenseFactory,
    LocationFactory,
    ProjectFactory,
    StockItemFactory,
    TenantFactory,
    UserFactory,
)
from apps.projects.models import ExpenseAssetLink
from apps.reservations.models import Checkout, Reservation
from apps.tenancy.context import tenant_context
from apps.tenancy.tests.test_cortex_app_runtime_rls import (
    _app_role_credentials,
    _as_cortex_app_role,
)

pytestmark = pytest.mark.django_db


def _run(tenant, *args, stdin: str | None = None, monkeypatch=None):
    if stdin is not None:
        monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
    if "--apply" in args and "--actor" not in args:
        # --actor is mandatory with --apply; default to an active tenant user.
        email = f"actor@{tenant.slug}.test"
        if not User.all_objects.filter(tenant=tenant, email=email).exists():
            UserFactory(tenant=tenant, email=email)
        args = (*args, "--actor", email)
    out, err = io.StringIO(), io.StringIO()
    code = 0
    try:
        call_command("convert_to_counted", "--tenant", tenant.slug, *args, stdout=out, stderr=err)
    except SystemExit as exc:
        code = int(exc.code or 0)
    return code, out.getvalue(), err.getvalue()


def _csv(*rows: tuple) -> str:
    return "id,quantity\n" + "".join(f"{a},{q}\n" for a, q in rows)


def _write(tmp_path, text: str) -> str:
    p = tmp_path / f"in{len(list(tmp_path.iterdir()))}.csv"
    p.write_text(text)
    return str(p)


def _state(asset):
    row = Asset.all_objects.get(pk=asset.pk)
    return row.tracking, row.quantity


@pytest.fixture
def lab():
    tenant = TenantFactory()
    cat = CategoryFactory(tenant=tenant, name="Compute")
    return tenant, cat


def _audits(tenant):
    return AuditLog.all_objects.filter(tenant=tenant, action="asset.edit")


class TestExport:
    def test_content_duplicates_and_blockers(self, lab):
        tenant, cat = lab
        other_cat = CategoryFactory(tenant=tenant, name="Tools")
        proj = ProjectFactory(tenant=tenant, name="Grant X")
        loc = LocationFactory(tenant=tenant, name="Shelf 1")
        a1 = AssetFactory(
            tenant=tenant,
            category=cat,
            name="  Jetson Nano ",
            project=proj,
            location=loc,
            serial_number="S1",
        )
        a2 = AssetFactory(tenant=tenant, category=cat, name="jetson nano")
        a3 = AssetFactory(tenant=tenant, category=other_cat, name="Jetson Nano")  # other category
        a4 = AssetFactory(tenant=tenant, category=cat, name="Unique")
        # Excluded: retired, consumable, already counted.
        AssetFactory(tenant=tenant, category=cat, name="Gone", status=Asset.Status.RETIRED)
        AssetFactory(tenant=tenant, category=cat, name="Glue", is_consumable=True)
        AssetFactory(tenant=tenant, category=cat, name="Counted", tracking="counted", quantity=3)
        user = UserFactory(tenant=tenant)
        start = timezone.now() + timedelta(hours=1)
        Reservation.all_objects.create(
            tenant=tenant,
            asset=a4,
            user=user,
            start_at=start,
            end_at=start + timedelta(hours=1),
            status="approved",
        )
        Checkout.all_objects.create(
            tenant=tenant,
            asset=a4,
            user=user,
            checked_out_at=timezone.now(),
            due_at=timezone.now() + timedelta(days=1),
        )
        StockItemFactory(
            tenant=tenant, asset=AssetFactory(tenant=tenant, category=cat, is_consumable=True)
        )

        code, out, _ = _run(tenant, "export-candidates")
        assert code == 0
        rows = list(csv.DictReader(io.StringIO(out)))
        assert list(rows[0]) == [
            "id",
            "uuid",
            "name",
            "category",
            "project",
            "location",
            "serial_number",
            "possible_duplicate_group",
            "blockers",
            "quantity",
        ]
        by_id = {int(r["id"]): r for r in rows}
        assert set(by_id) == {a1.id, a2.id, a3.id, a4.id}
        assert by_id[a1.id]["project"] == "Grant X"
        assert by_id[a1.id]["location"] == "Shelf 1"
        assert by_id[a1.id]["serial_number"] == "S1"
        assert by_id[a1.id]["uuid"] == str(a1.uuid)
        assert (
            by_id[a1.id]["possible_duplicate_group"]
            == by_id[a2.id]["possible_duplicate_group"]
            != ""
        )
        assert by_id[a3.id]["possible_duplicate_group"] == ""
        assert by_id[a4.id]["possible_duplicate_group"] == ""
        assert "active reservation" in by_id[a4.id]["blockers"]
        assert "open checkout" in by_id[a4.id]["blockers"]
        assert by_id[a1.id]["blockers"] == ""
        assert all(r["quantity"] == "" for r in rows)

    def test_only_this_tenants_assets(self, lab):
        tenant, cat = lab
        mine = AssetFactory(tenant=tenant, category=cat)
        theirs = AssetFactory()
        _, out, _ = _run(tenant, "export-candidates")
        ids = {int(r["id"]) for r in csv.DictReader(io.StringIO(out))}
        assert ids == {mine.id} and theirs.id not in ids

    def test_output_file(self, lab, tmp_path):
        tenant, cat = lab
        a = AssetFactory(tenant=tenant, category=cat)
        path = tmp_path / "c.csv"
        code, out, _ = _run(tenant, "export-candidates", "-o", str(path))
        assert code == 0 and out == ""
        assert str(a.id) in path.read_text()


class TestDryRun:
    def test_dry_run_writes_nothing(self, lab, tmp_path):
        tenant, cat = lab
        a = AssetFactory(tenant=tenant, category=cat)
        code, out, _ = _run(tenant, "apply", _write(tmp_path, _csv((a.id, 5))))
        assert code == 0
        assert "OK" in out and "individual/1 -> counted/5" in out
        assert "Dry run only" in out
        assert _state(a) == ("individual", 1)
        assert not _audits(tenant).exists()

    def test_blank_quantity_skipped(self, lab, tmp_path):
        tenant, cat = lab
        a, b = AssetFactory(tenant=tenant, category=cat), AssetFactory(tenant=tenant, category=cat)
        code, out, _ = _run(tenant, "apply", _write(tmp_path, _csv((a.id, ""), (b.id, 2))))
        assert code == 0
        assert "1 skipped" in out and "1 OK" in out

    def test_uuid_column_accepted(self, lab, tmp_path):
        tenant, cat = lab
        a = AssetFactory(tenant=tenant, category=cat)
        code, out, _ = _run(
            tenant, "apply", "--apply", _write(tmp_path, f"uuid,quantity\n{a.uuid},4\n")
        )
        assert code == 0
        assert _state(a) == ("counted", 4)

    def test_stdin_input(self, lab, monkeypatch):
        tenant, cat = lab
        a = AssetFactory(tenant=tenant, category=cat)
        code, out, _ = _run(
            tenant, "apply", "-", "--apply", stdin=_csv((a.id, 7)), monkeypatch=monkeypatch
        )
        assert code == 0
        assert _state(a) == ("counted", 7)


class TestApply:
    def test_converts_and_audits(self, lab, tmp_path):
        tenant, cat = lab
        actor = UserFactory(tenant=tenant, email="ops@lab.test")
        a, b = AssetFactory(tenant=tenant, category=cat), AssetFactory(tenant=tenant, category=cat)
        code, out, _ = _run(
            tenant,
            "apply",
            "--apply",
            "--actor",
            "OPS@lab.test",
            _write(tmp_path, _csv((a.id, 10), (b.id, 3))),
        )
        assert code == 0, out
        assert _state(a) == ("counted", 10) and _state(b) == ("counted", 3)
        assert "Financial figures unchanged (0 projects, 0 expenses)" in out
        entry = _audits(tenant).get(entity_id=str(a.id))
        assert entry.actor_id == actor.id
        assert entry.before == {"tracking": "individual", "quantity": 1, "is_consumable": False}
        assert entry.after == {"tracking": "counted", "quantity": 10, "is_consumable": False}
        assert _audits(tenant).count() == 2

    def test_actor_required_with_apply(self, lab, tmp_path):
        from django.core.management.base import CommandError

        tenant, cat = lab
        a = AssetFactory(tenant=tenant, category=cat)
        with pytest.raises(CommandError, match="--actor"):
            call_command(
                "convert_to_counted",
                "--tenant",
                tenant.slug,
                "apply",
                "--apply",
                _write(tmp_path, _csv((a.id, 2))),
            )
        assert _state(a) == ("individual", 1)

    def test_inactive_or_other_tenant_actor_rejected(self, lab, tmp_path):
        from django.core.management.base import CommandError

        tenant, cat = lab
        a = AssetFactory(tenant=tenant, category=cat)
        UserFactory(tenant=tenant, email="off@x.test", is_active=False)
        UserFactory(email="alien@x.test")
        for email in ("off@x.test", "alien@x.test"):
            with pytest.raises(CommandError, match="active user"):
                call_command(
                    "convert_to_counted",
                    "--tenant",
                    tenant.slug,
                    "apply",
                    "--apply",
                    "--actor",
                    email,
                    _write(tmp_path, _csv((a.id, 2))),
                )
        assert _state(a) == ("individual", 1)

    def test_unknown_actor_errors(self, lab, tmp_path):
        tenant, cat = lab
        a = AssetFactory(tenant=tenant, category=cat)
        from django.core.management.base import CommandError

        with pytest.raises(CommandError):
            call_command(
                "convert_to_counted",
                "--tenant",
                tenant.slug,
                "apply",
                "--apply",
                "--actor",
                "nobody@x.test",
                _write(tmp_path, _csv((a.id, 2))),
            )
        assert _state(a) == ("individual", 1)

    def test_counted_to_counted_update_reported(self, lab, tmp_path):
        tenant, cat = lab
        a = AssetFactory(tenant=tenant, category=cat, tracking="counted", quantity=4)
        code, out, _ = _run(tenant, "apply", "--apply", _write(tmp_path, _csv((a.id, 9))))
        assert code == 0
        assert "already counted" in out and "counted/4 -> counted/9" in out
        assert _state(a) == ("counted", 9)

    def test_any_error_applies_nothing(self, lab, tmp_path):
        tenant, cat = lab
        good = AssetFactory(tenant=tenant, category=cat)
        bad = AssetFactory(tenant=tenant, category=cat, is_consumable=True)
        code, out, _ = _run(
            tenant, "apply", "--apply", _write(tmp_path, _csv((good.id, 5), (bad.id, 2)))
        )
        assert code == 1
        assert "ERROR" in out and "nothing was changed" in out
        assert _state(good) == ("individual", 1)
        assert not _audits(tenant).exists()

    @pytest.mark.parametrize("qty", ["0", "-2", "abc", "1.5"])
    def test_bad_quantity_is_row_error(self, lab, tmp_path, qty):
        tenant, cat = lab
        a = AssetFactory(tenant=tenant, category=cat)
        code, out, _ = _run(tenant, "apply", _write(tmp_path, _csv((a.id, qty))))
        assert code == 1 and "positive integer" in out

    def test_duplicate_ids_error(self, lab, tmp_path):
        tenant, cat = lab
        a = AssetFactory(tenant=tenant, category=cat)
        code, out, _ = _run(
            tenant, "apply", "--apply", _write(tmp_path, _csv((a.id, 2), (a.id, 3)))
        )
        assert code == 1 and "duplicate" in out
        assert _state(a) == ("individual", 1)

    def test_missing_columns(self, lab, tmp_path):
        from django.core.management.base import CommandError

        tenant, _ = lab
        with pytest.raises(CommandError):
            call_command(
                "convert_to_counted",
                "--tenant",
                tenant.slug,
                "apply",
                _write(tmp_path, "name,quantity\nx,1\n"),
            )


class TestRefusals:
    def test_blocked_rows_refused(self, lab, tmp_path):
        tenant, cat = lab
        user = UserFactory(tenant=tenant)
        consumable = AssetFactory(tenant=tenant, category=cat, is_consumable=True)
        reserved = AssetFactory(tenant=tenant, category=cat)
        out_ = AssetFactory(tenant=tenant, category=cat)
        stocked = StockItemFactory(
            tenant=tenant, asset=AssetFactory(tenant=tenant, category=cat, is_consumable=True)
        ).asset
        # Legacy shape: a durable asset that still carries a stock record.
        Asset.all_objects.filter(pk=stocked.pk).update(is_consumable=False)
        retired = AssetFactory(tenant=tenant, category=cat, status=Asset.Status.RETIRED)
        start = timezone.now() + timedelta(hours=1)
        Reservation.all_objects.create(
            tenant=tenant,
            asset=reserved,
            user=user,
            start_at=start,
            end_at=start + timedelta(hours=1),
            status="pending",
        )
        Checkout.all_objects.create(
            tenant=tenant,
            asset=out_,
            user=user,
            checked_out_at=timezone.now(),
            due_at=timezone.now() + timedelta(days=1),
        )
        rows = [(x.id, 2) for x in (consumable, reserved, out_, stocked, retired)]
        code, out, _ = _run(tenant, "apply", "--apply", _write(tmp_path, _csv(*rows)))
        assert code == 1
        assert out.count("ERROR") == 5
        assert "consumable can't be counted" in out
        assert "active reservations" in out
        assert "checked out" in out
        assert "stock record" in out
        assert "Retired" in out
        for x in (consumable, reserved, out_, stocked, retired):
            assert _state(x)[0] == "individual"

    def test_other_tenants_id_rejected(self, lab, tmp_path):
        tenant, cat = lab
        mine = AssetFactory(tenant=tenant, category=cat)
        theirs = AssetFactory()
        code, out, _ = _run(
            tenant, "apply", "--apply", _write(tmp_path, _csv((mine.id, 2), (theirs.id, 2)))
        )
        assert code == 1 and "unknown asset" in out
        assert _state(mine) == ("individual", 1) and _state(theirs) == ("individual", 1)

    def test_unknown_tenant(self):
        from django.core.management.base import CommandError

        with pytest.raises(CommandError):
            call_command("convert_to_counted", "--tenant", "nope", "export-candidates")


def _money(tenant, cat):
    project = ProjectFactory(tenant=tenant, budget_total=Decimal("1000.00"))
    a1, a2 = AssetFactory(tenant=tenant, category=cat), AssetFactory(tenant=tenant, category=cat)
    exp = ExpenseFactory(tenant=tenant, project=project, amount="100.00")
    ExpenseAssetLink.all_objects.create(
        tenant=tenant, expense=exp, asset=a1, allocated_amount=Decimal("30.00"), quantity=1
    )
    ExpenseAssetLink.all_objects.create(tenant=tenant, expense=exp, asset=a2, quantity=1)
    return a1, a2, exp


class TestFinancialGuard:
    def test_unchanged_figures_reported(self, lab, tmp_path):
        tenant, cat = lab
        a1, a2, _ = _money(tenant, cat)
        code, out, _ = _run(
            tenant, "apply", "--apply", _write(tmp_path, _csv((a1.id, 2), (a2.id, 2)))
        )
        assert code == 0
        assert "Financial figures unchanged (1 projects, 1 expenses)" in out
        assert _state(a1) == ("counted", 2)

    def test_figure_change_rolls_everything_back(self, lab, tmp_path, monkeypatch):
        tenant, cat = lab
        a1, a2, exp = _money(tenant, cat)
        real = conversion.write_audit_log

        def tampering(**kw):
            entry = real(**kw)
            ExpenseAssetLink.all_objects.filter(
                expense=exp, allocated_amount=Decimal("30.00")
            ).update(allocated_amount=Decimal("31.00"))
            return entry

        monkeypatch.setattr(conversion, "write_audit_log", tampering)
        code, out, err = _run(
            tenant, "apply", "--apply", _write(tmp_path, _csv((a1.id, 2), (a2.id, 2)))
        )
        assert code == 1
        assert "FINANCIAL GUARD TRIPPED" in err
        assert "30.00" in err and "31.00" in err
        assert _state(a1) == ("individual", 1) and _state(a2) == ("individual", 1)
        assert not _audits(tenant).exists()
        assert ExpenseAssetLink.all_objects.get(expense=exp, asset=a1).allocated_amount == Decimal(
            "30.00"
        )

    def test_rollup_change_trips_guard(self, lab, tmp_path, monkeypatch):
        tenant, cat = lab
        a1, _, exp = _money(tenant, cat)
        real = conversion.write_audit_log

        def tampering(**kw):
            from apps.projects.models import Expense

            Expense.all_objects.filter(pk=exp.pk).update(amount=Decimal("101.00"))
            return real(**kw)

        monkeypatch.setattr(conversion, "write_audit_log", tampering)
        code, _, err = _run(tenant, "apply", "--apply", _write(tmp_path, _csv((a1.id, 2))))
        assert code == 1 and "FINANCIAL GUARD" in err
        assert _state(a1) == ("individual", 1)


@pytest.mark.django_db(transaction=True)
def test_runs_end_to_end_as_cortex_app(tmp_path):
    tenant = TenantFactory()
    other = TenantFactory()
    cat = CategoryFactory(tenant=tenant)
    a, _, _ = _money(tenant, cat)
    theirs = AssetFactory(tenant=other)
    actor_email = f"actor@{tenant.slug}.test"
    UserFactory(tenant=tenant, email=actor_email)
    path = _write(tmp_path, _csv((a.id, 6)))
    bad = _write(tmp_path, _csv((theirs.id, 6)))

    with _as_cortex_app_role():
        from django.db import connection

        with connection.cursor() as cur:
            cur.execute("SELECT current_user, rolsuper FROM pg_roles WHERE rolname = current_user")
            assert cur.fetchone() == (_app_role_credentials()[0], False)

        code, out, _ = _run(tenant, "export-candidates")
        ids = {int(r["id"]) for r in csv.DictReader(io.StringIO(out))}
        assert code == 0 and a.id in ids and theirs.id not in ids
        code, out, _ = _run(tenant, "apply", path)
        assert code == 0, out
        code, out, _ = _run(tenant, "apply", "--apply", "--actor", actor_email, bad)
        assert code == 1 and "unknown asset" in out
        code, out, _ = _run(tenant, "apply", "--apply", "--actor", actor_email, path)
        assert code == 0, out
        assert "Financial figures unchanged (1 projects, 1 expenses)" in out

    with tenant_context(tenant.id):
        assert Asset.objects.get(pk=a.pk).quantity == 6
        assert AuditLog.objects.filter(action="asset.edit").count() == 1
    assert _state(theirs) == ("individual", 1)


class TestInputHardening:
    def test_quantity_upper_bound(self, lab, tmp_path):
        tenant, cat = lab
        a = AssetFactory(tenant=tenant, category=cat)
        code, out, _ = _run(tenant, "apply", _write(tmp_path, _csv((a.id, 2147483648))))
        assert code == 1 and "too large" in out
        code, out, _ = _run(tenant, "apply", _write(tmp_path, _csv((a.id, 2147483647))))
        assert code == 0

    def test_excel_float_ids_and_quantities_accepted(self, lab, tmp_path):
        tenant, cat = lab
        a = AssetFactory(tenant=tenant, category=cat)
        code, out, _ = _run(
            tenant, "apply", "--apply", _write(tmp_path, f"id,quantity\n{a.id}.0,5.0\n")
        )
        assert code == 0, out
        assert _state(a) == ("counted", 5)

    @pytest.mark.parametrize("bad", ["²", "1e3", "١٢", "1.5", "0x10"])
    def test_weird_numerals_are_row_errors_not_tracebacks(self, lab, tmp_path, bad):
        tenant, cat = lab
        a = AssetFactory(tenant=tenant, category=cat)
        code, out, _ = _run(
            tenant, "apply", _write(tmp_path, f"id,quantity\n{bad},2\n{a.id},{bad}\n")
        )
        assert code == 1 and out.count("ERROR") == 2

    def test_bom_on_stdin_and_file(self, lab, tmp_path, monkeypatch):
        tenant, cat = lab
        a = AssetFactory(tenant=tenant, category=cat)
        raw = ("﻿" + _csv((a.id, 3))).encode("utf-8")
        monkeypatch.setattr(
            sys, "stdin", io.TextIOWrapper(io.BytesIO(raw), encoding="utf-8", newline="")
        )
        code, out, _ = _run(tenant, "apply", "-")
        assert code == 0, out
        p = tmp_path / "bom.csv"
        p.write_bytes(raw)
        code, out, _ = _run(tenant, "apply", str(p))
        assert code == 0, out
        # StringIO path (text stdin that kept the BOM)
        code, out, _ = _run(
            tenant, "apply", "-", stdin="﻿" + _csv((a.id, 3)), monkeypatch=monkeypatch
        )
        assert code == 0, out

    def test_non_utf8_is_clean_error(self, lab, tmp_path, monkeypatch):
        from django.core.management.base import CommandError

        tenant, cat = lab
        a = AssetFactory(tenant=tenant, category=cat)
        raw = f"id,name,quantity\n{a.id},caf\xe9,2\n".encode("cp1252")
        p = tmp_path / "x.csv"
        p.write_bytes(raw)
        with pytest.raises(CommandError, match="CSV UTF-8"):
            call_command("convert_to_counted", "--tenant", tenant.slug, "apply", str(p))
        monkeypatch.setattr(
            sys, "stdin", io.TextIOWrapper(io.BytesIO(raw), encoding="utf-8", newline="")
        )
        with pytest.raises(CommandError, match="CSV UTF-8"):
            call_command("convert_to_counted", "--tenant", tenant.slug, "apply", "-")

    def test_id_uuid_mismatch(self, lab, tmp_path):
        tenant, cat = lab
        a, b = AssetFactory(tenant=tenant, category=cat), AssetFactory(tenant=tenant, category=cat)
        text = f"id,uuid,quantity\n{a.id},{b.uuid},2\n{a.id},{a.uuid},2\n"
        code, out, _ = _run(tenant, "apply", _write(tmp_path, text))
        assert code == 1
        assert "don't match" in out
        assert out.count("ERROR") == 1

    def test_name_column_must_match(self, lab, tmp_path):
        tenant, cat = lab
        a = AssetFactory(tenant=tenant, category=cat, name="Cable")
        b = AssetFactory(tenant=tenant, category=cat, name="Hub")
        ok = f"id,name,quantity\n{a.id}, CABLE ,2\n"
        assert _run(tenant, "apply", _write(tmp_path, ok))[0] == 0
        bad = f"id,name,quantity\n{a.id},Hub,2\n{b.id},Hub,2\n"
        code, out, _ = _run(tenant, "apply", "--apply", _write(tmp_path, bad))
        assert code == 1 and "doesn't match" in out
        assert _state(a) == ("individual", 1) and _state(b) == ("individual", 1)

    def test_line_numbers_follow_the_file(self, lab, tmp_path):
        tenant, cat = lab
        a = AssetFactory(tenant=tenant, category=cat)
        # A quoted multi-line cell pushes the bad row to physical line 5.
        text = f'id,name,quantity\n{a.id},"{a.name}",\n"x\ny",z,\n99999,zz,3\n'
        code, out, _ = _run(tenant, "apply", _write(tmp_path, text))
        assert code == 1 and "line 5 " in out


class TestNoopAndRaces:
    def test_unchanged_rows_not_saved_or_audited(self, lab, tmp_path):
        tenant, cat = lab
        same = AssetFactory(tenant=tenant, category=cat, tracking="counted", quantity=4)
        change = AssetFactory(tenant=tenant, category=cat)
        before = Asset.all_objects.get(pk=same.pk).updated_at
        code, out, _ = _run(
            tenant, "apply", "--apply", _write(tmp_path, _csv((same.id, 4), (change.id, 2)))
        )
        assert code == 0
        assert "unchanged" in out and "converted 1 assets (1 unchanged)" in out
        assert Asset.all_objects.get(pk=same.pk).updated_at == before
        assert list(_audits(tenant).values_list("entity_id", flat=True)) == [str(change.id)]

    def test_asset_vanishing_mid_apply_rolls_back(self, lab, tmp_path, monkeypatch):
        tenant, cat = lab
        a, b = AssetFactory(tenant=tenant, category=cat), AssetFactory(tenant=tenant, category=cat)
        real = conversion.financial_fingerprint
        calls: list[int] = []

        def vanish():
            if not calls:
                calls.append(1)
                Asset.all_objects.filter(pk=b.pk).delete()
            return real()

        monkeypatch.setattr(conversion, "financial_fingerprint", vanish)
        code, out, err = _run(
            tenant, "apply", "--apply", _write(tmp_path, _csv((a.id, 2), (b.id, 2)))
        )
        assert code == 1 and "rolled back" in err
        assert _state(a) == ("individual", 1)
        assert not _audits(tenant).exists()
