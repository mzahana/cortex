"""Bulk-convert EXISTING assets to `tracking=counted`, safely.

    python manage.py convert_to_counted --tenant <slug> export-candidates [-o FILE|-]
    python manage.py convert_to_counted --tenant <slug> apply <CSV|-> [--apply] [--actor EMAIL]

Runs as the RLS-subject `cortex_app` role (the `web` container): all ORM work
is inside `tenant_context(tenant.id)`. `apply` is a DRY RUN unless `--apply` is
given; with `--apply` it is all-or-nothing and guarded by a before/after
fingerprint of every project financial figure. See
`docs/deployment-runbook.md` ("Converting existing assets to Counted") and
`apps.assets.conversion` for the logic.

Exit codes: 0 success / clean dry run, 1 any error.
"""

from __future__ import annotations

import csv
import io
import sys
from typing import Any

from django.core.exceptions import ObjectDoesNotExist
from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db import DatabaseError
from rest_framework import serializers

from apps.accounts.models import User
from apps.assets import conversion
from apps.assets.services import counted_blockers
from apps.tenancy.context import tenant_context
from apps.tenancy.models import Tenant

EXPORT_COLUMNS = [
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


class Command(BaseCommand):
    help = "Bulk-convert existing individual assets to counted (export, dry-run, apply)."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--tenant", required=True, help="Tenant slug.")
        sub = parser.add_subparsers(dest="action", required=True)
        exp = sub.add_parser("export-candidates", help="Write a CSV of convertible assets.")
        exp.add_argument("-o", "--output", default="-", help="Output file, or - for stdout.")
        app = sub.add_parser("apply", help="Validate (and with --apply, perform) conversions.")
        app.add_argument("csv_path", help="CSV file, or - for stdin.")
        app.add_argument(
            "--apply",
            action="store_true",
            dest="do_apply",
            help="Write changes (default: dry run).",
        )
        app.add_argument("--actor", default=None, help="Email of the tenant user to audit as.")

    def handle(self, *args: Any, **opts: Any) -> None:
        try:
            tenant = Tenant.objects.get(slug=opts["tenant"])
        except Tenant.DoesNotExist:
            raise CommandError(f"No tenant with slug {opts['tenant']!r}.") from None
        with tenant_context(tenant.id):
            if opts["action"] == "export-candidates":
                self._export(opts["output"])
            else:
                self._apply(tenant, opts)

    # -- export ---------------------------------------------------------------

    def _export(self, output: str) -> None:
        assets = conversion.candidate_assets()
        groups = conversion.duplicate_groups(assets)
        buf = io.StringIO()
        writer = csv.writer(buf, lineterminator="\n")
        writer.writerow(EXPORT_COLUMNS)
        for a in assets:
            blockers = "; ".join(label for label, _ in counted_blockers(a))
            writer.writerow(
                [
                    a.id,
                    a.uuid,
                    a.name,
                    a.category.name,
                    a.project.name if a.project else "",
                    a.location.name if a.location else "",
                    a.serial_number or "",
                    groups.get(a.id, ""),
                    blockers,
                    "",
                ]
            )
        if output == "-":
            self.stdout.write(buf.getvalue(), ending="")
        else:
            with open(output, "w", newline="", encoding="utf-8") as fh:
                fh.write(buf.getvalue())
        self.stderr.write(f"Exported {len(assets)} candidate assets.")

    # -- apply ----------------------------------------------------------------

    def _read_rows(self, path: str) -> list[dict[str, str]]:
        hint = "Save the sheet as 'CSV UTF-8'."
        try:
            if path == "-":
                buf = getattr(sys.stdin, "buffer", None)
                text = (buf.read().decode("utf-8-sig")) if buf else sys.stdin.read()
            else:
                with open(path, "rb") as fh:
                    text = fh.read().decode("utf-8-sig")
        except UnicodeDecodeError:
            raise CommandError(f"Input isn't valid UTF-8. {hint}") from None
        except OSError as exc:
            raise CommandError(f"Can't read {path}: {exc}") from None
        text = text.lstrip("\ufeff")
        reader = csv.DictReader(io.StringIO(text, newline=""))
        fields = {(f or "").strip().lower() for f in (reader.fieldnames or [])}
        if "quantity" not in fields or not ({"id", "uuid"} & fields):
            raise CommandError("CSV needs a 'quantity' column and an 'id' (or 'uuid') column.")
        rows = []
        for row in reader:
            clean = {(k or "").strip().lower(): (v or "") for k, v in row.items() if k}
            clean["_line"] = str(reader.line_num)
            rows.append(clean)
        return rows

    def _apply(self, tenant: Tenant, opts: dict[str, Any]) -> None:
        actor = None
        if opts["do_apply"] and not opts["actor"]:
            raise CommandError(
                "--actor EMAIL is required with --apply (an active user in the tenant)."
            )
        if opts["actor"]:
            actor = User.objects.filter(email__iexact=opts["actor"], is_active=True).first()
            if actor is None:
                raise CommandError(f"No active user {opts['actor']!r} in tenant {tenant.slug!r}.")
        plan = conversion.plan_rows(self._read_rows(opts["csv_path"]))
        out = self.stdout.write
        for rp in plan.rows:
            name = rp.asset.name if rp.asset else "?"
            if rp.ok:
                assert rp.before is not None and rp.after is not None
                note = f" ({rp.note})" if rp.note else ""
                out(
                    f"line {rp.line} id={rp.raw_id} {name!r}: OK{note} "
                    f"{rp.before['tracking']}/{rp.before['quantity']} -> "
                    f"{rp.after['tracking']}/{rp.after['quantity']}"
                )
            else:
                out(f"line {rp.line} id={rp.raw_id} {name!r}: ERROR {rp.error}")
        n_err = len(plan.errors)
        out(
            f"Summary: {len(plan.rows) - n_err} OK ({len(plan.to_convert)} to change), "
            f"{n_err} errors, {plan.skipped} skipped (blank quantity)."
        )
        if n_err:
            out(
                "Errors found; nothing was changed."
                if opts["do_apply"]
                else "Dry run: errors found."
            )
            raise SystemExit(1)
        if not opts["do_apply"]:
            out("Dry run only; re-run with --apply to write these changes.")
            return
        try:
            n_proj, n_exp = conversion.apply_conversions(plan, tenant_id=tenant.id, actor=actor)
        except conversion.FinancialGuardError as exc:
            self.stderr.write("FINANCIAL GUARD TRIPPED; everything rolled back:")
            for line in exc.diff:
                self.stderr.write(f"  {line}")
            raise SystemExit(1) from None
        except (serializers.ValidationError, DatabaseError, ObjectDoesNotExist) as exc:
            self.stderr.write(f"Apply failed; everything rolled back: {exc}")
            raise SystemExit(1) from None
        out(f"Financial figures unchanged ({n_proj} projects, {n_exp} expenses).")
        out(
            f"Applied: converted {len(plan.to_convert)} assets "
            f"({len(plan.rows) - len(plan.to_convert)} unchanged)."
        )
