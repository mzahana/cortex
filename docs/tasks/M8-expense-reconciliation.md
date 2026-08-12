# M8 — Expense reconciliation, multi-currency & audit PDF packs

> Post-MVP feature milestone. M7 gave every project a budget and an itemized
> expense ledger. M8 makes that ledger survive a **real audit**, where the
> paper trail does not line up one-to-one: one bank debit pays for several
> vendor receipts, one receipt contains several items destined for different
> projects, one item becomes several assets, one asset is used by several
> projects, and the receipt is in USD while the bank statement is in SAR.
>
> **Status: Phase 1 built, tested and merged to the working tree; Phases 2–4
> planned, not built.** Unlike the historical M0–M7 task docs, the un-built
> phases here are still a forward-looking build plan — see §9 for which is
> which.

## Why / user story
The lab lead orders five items from Amazon in one checkout. The bank statement
shows a single deduction — **SAR 4,631.10** — but Amazon splits the order into
three shipments, each with its own receipt, each priced in **USD**. Two items
belong to Grant A, three to Project B. One item becomes four identical assets.
A drone bought under Grant A is used by Project B for six months.

At audit, the lead must show, for each line in the bank statement, which
receipts it paid for, which items were on those receipts, what each item cost
in the funding currency, which project it was booked to, and a photo of the
thing itself. Today that reconciliation happens in the lead's head and in a
spreadsheet. M8 moves it into the system.

## Root cause in the current model
Two entities are each doing several jobs at once:

1. **`projects.Expense` is simultaneously the money movement, the receipt, and
   the line item.** That identity only holds when 1 payment = 1 receipt = 1
   item = 1 project. Every scenario above breaks it, and no additional column
   on `Expense` repairs it.
2. **`assets.Asset.project` is simultaneously "who funded it" and "who uses
   it".** These genuinely differ, and conflating them either loses the funding
   record or double-counts an asset's cost across grants — the latter being a
   finding an auditor will act on.

M8 splits each conflated concept into its own record. Everything else follows.

## Scope decisions (confirmed with the user)
- **Ease of use and audit-legible reports are the acceptance bar**, not model
  purity. The user is explicitly not making the engineering calls; defaults
  below are chosen for them and flagged as assumptions in §10.
- **Reconciliation checks warn, never block.** Real receipts carry roundings,
  partial refunds and gift-card offsets; the app must never refuse the user's
  data because the arithmetic is 0.03 off.
- **FX rate is derived from the actual bank debit** (§3), not from a published
  rate table. No external FX API, no scheduled rate sync, no new dependency.
- **Funding stays single-project.** An asset is funded by exactly one project;
  usage is many. Split/co-funding of a single line across projects is
  explicitly out of scope (see §10 for the extension point).
- **Existing data keeps working untouched.** Every new FK is nullable; an M7
  expense with no payment and no receipt remains valid forever.

## Non-negotiables (same as every milestone — see CLAUDE.md)
- Every new tenant-owned table subclasses `TenantScopedModel` and ships a
  fail-closed Postgres RLS policy in the same migration series. `Payment` and
  `Purchase` are **tenant-scoped but NOT project-scoped** — see §7, this is the
  one genuinely new RBAC shape in M8 and the milestone's main R4 risk.
- RBAC server-side on every endpoint, after tenant isolation, using the new
  permission keys in §7.
- Every mutating action audited with immutable before/after.
- All PDF rendering runs in **Celery** via the existing `apps.jobs` poller —
  never in the request cycle.
- Attachment binaries live on the storage backend; only `storage_key` +
  metadata in the DB. Reuse `apps.assets.services.save_attachment_file` as the
  only writer of `storage_key`.
- Lists server-side paginated/filtered; query budgets asserted.

---

## 1. Data model

### 1.1 New app: `apps/finance`
`Payment` and `Purchase` span projects, so they do not belong in
`apps.projects` (whose M7 tables are all project-scoped). One Django app per
domain area, per `architecture.md`. `Expense` **stays in `apps.projects`** —
moving it across apps is migration pain for no benefit — and gains a
cross-app FK to `finance.Purchase`.

### 1.2 `finance.Payment` — the money movement
One row per line on the bank statement. Tenant-scoped.

| field | notes |
|---|---|
| `amount` | Decimal(14,2), **settlement currency** — what the bank actually took |
| `currency` | 3 chars, e.g. `SAR` |
| `paid_on` | date of the debit |
| `method` | `card` \| `bank_transfer` \| `cash` \| `other` |
| `account_label` | free text, e.g. `Visa •4321` — never a full PAN |
| `statement_ref` | the bank's own reference, the auditor's join key |
| `vendor` | denormalized convenience for search |
| `notes` | free text |
| `created_by`, timestamps | as M7 |

`PaymentAttachment` — same storage convention as `ExpenseAttachment`. Holds
the **bank statement excerpt**. `kind` = `statement` \| `other`.

Indexes: `(tenant, paid_on)`, `(tenant, vendor)`.

### 1.3 `finance.Purchase` — one vendor receipt
One row per receipt / split shipment / sub-order. Tenant-scoped.

| field | notes |
|---|---|
| `payment` | FK nullable, `SET_NULL` — a receipt may be logged before its charge is known |
| `vendor`, `vendor_order_number`, `receipt_number`, `date` | |
| `subtotal`, `shipping`, `tax`, `total` | Decimal(14,2), all in **transaction currency** |
| `currency` | 3 chars, e.g. `USD` |
| `settled_amount` | Decimal(14,2), nullable, in the payment's currency — see §3 |
| `fx_rate` | Decimal(18,8), nullable, **stored not computed** — see §3 |
| `notes`, `created_by`, timestamps | |

`PurchaseAttachment` — **the receipt scan lives here**, not on the line item.
This is what stops the same PDF being repeated once per item in the report
appendix, which is the single ugliest thing about a hand-assembled audit pack.

Indexes: `(tenant, payment)`, `(tenant, date)`, `(tenant, vendor)`.
No uniqueness on `vendor_order_number` — split shipments deliberately share it.

### 1.4 `projects.Expense` — becomes the line item
Additive only:

- `purchase` — FK to `finance.Purchase`, nullable, `SET_NULL`.
- `allocated_overhead` — Decimal(14,2), default 0, transaction currency. The
  line's stored share of the receipt's shipping + tax (§4).
- `amount_settled` — Decimal(14,2), nullable, settlement currency. Stored, not
  computed (§3).
- `quantity` — PositiveInteger, default 1.

`amount` + `currency` keep their meaning (transaction currency). The
**fully-loaded line cost** an auditor cares about is
`amount + allocated_overhead`, in transaction currency, or `amount_settled` in
settlement currency.

### 1.5 `projects.ExpenseAssetLink` — an expense line ↔ many assets
Replaces the single `Expense.asset` FK.

| field | notes |
|---|---|
| `expense`, `asset` | FKs, unique together with `tenant` |
| `allocated_amount` | Decimal(14,2), nullable — this asset's share of the line |
| `quantity` | default 1 |

Backfill: every existing `Expense.asset` becomes one link row with
`allocated_amount = amount`. `Expense.asset` is kept for **one release** as a
deprecated read-only property so the frontend and
`test_expense_prefill_from_asset.py` migrate without a flag day, then dropped.

### 1.6 `assets.AssetProjectUsage` — an asset ↔ many projects
| field | notes |
|---|---|
| `asset`, `project` | FKs |
| `start_date`, `end_date` | end nullable = ongoing |
| `note` | |

`Asset.project` is **unchanged in the database** and relabelled everywhere in
the UI and docs as the **funding project**. No column rename — the churn
across `assets`, `dashboard`, `imports` and the RBAC scope rule is not worth
it, and the field's meaning was always the funding/custodial owner.

> **Load-bearing rule: usage links never affect money.** Project spend is
> always `sum(Expense.amount_settled where project = P)`. Usage appears only in
> clearly-marked non-cost report sections. Violating this double-counts
> equipment across grants.

---

## 2. Reconciliation rules
Two soft invariants, computed on read, surfaced as badges. Never enforced as
DB constraints.

1. **Payment balanced** — `sum(purchase.settled_amount) == payment.amount`.
2. **Receipt balanced** — `sum(line.amount) + shipping + tax == purchase.total`.

Each resolves to `balanced` / `under` / `over` with the signed variance. A
tenant-wide **Reconciliation** screen lists every payment by state; that list
*is* the audit-readiness to-do list, and the audit-readiness PDF (§6) is a
render of it.

---

## 3. Multi-currency

Three distinct currency roles, deliberately named apart:

| role | example | where it lives |
|---|---|---|
| **Transaction** | USD — what the receipt says | `Purchase.currency`, `Expense.currency` |
| **Settlement** | SAR — what the bank actually took | `Payment.currency` |
| **Reporting** | SAR — the grant's currency | `Project.currency` (exists, M7) |

**The rate is derived from the debit, never looked up.** If a payment of
SAR 1,500.00 settles a single USD 400.00 receipt, the effective rate is
`3.75000000` — and it already contains the bank's FX markup and any foreign
transaction fee. A published mid-market rate would leave a residual that never
balances, which is exactly the unexplained gap an auditor asks about. So:

```
purchase.fx_rate       = payment.amount / sum(totals of that payment's
                                              purchases in that currency)
purchase.settled_amount = purchase.total * purchase.fx_rate
expense.amount_settled  = (amount + allocated_overhead) * purchase.fx_rate
```

Cases the code must handle:

1. **Same currency** (receipt SAR, bank SAR) → `fx_rate = 1`, no UI shown at
   all. Must stay invisible for the common case.
2. **One payment, all receipts in one foreign currency** → the app derives one
   rate and shows it: *"Rate used: 3.750000 SAR/USD, derived from your bank
   debit."*
3. **One payment, mixed currencies** → the app cannot derive it; the user
   enters each receipt's `settled_amount` (readable off the statement or the
   card app), and the app derives a per-receipt rate. Sum must equal the debit,
   or the payment badge goes amber.
4. **Receipt with no payment yet** → `fx_rate` and `amount_settled` stay NULL;
   the line reports in transaction currency and the project rollup flags it as
   *unsettled*. It is not an error, it is a state.
5. **Project currency ≠ settlement currency** (rare: SAR grant, USD bank
   account) → an explicit `fx_rate_to_reporting` on the payment, entered by the
   user. Default assumption is that they are equal; a mismatch warns.

**Rates and settled amounts are stored, never recomputed on read.** A report
regenerated in three years must reproduce the same numbers even if a line was
later edited. Editing a payment's amount re-derives and re-stores the rates for
its purchases, and writes an audit entry showing the before/after.

**Rounding**: every proportional split (FX, shipping, tax) uses
**largest-remainder allocation** so the parts sum *exactly* to the whole. A
0.01 that vanishes is a 0.01 an auditor will find.

**Display rule, everywhere in UI and PDF**: original first, converted in
parentheses with the rate —
`USD 350.00 (SAR 1,312.50 @ 3.750000)`. Never show only the converted figure;
the auditor is holding the USD receipt.

**Asset cost**: `Asset.purchase_cost`/`currency` keep transaction currency;
the settled figure is read through the expense link. No change to the assets
schema.

---

## 4. Shipping, tax and fees
Entered once on the `Purchase`. Default handling is **pro-rata by line
amount**, materialized into each line's stored `allocated_overhead`. Rationale:
it makes each line's fully-loaded cost correct (a $350 GPU on a receipt with
$30 shipping really did cost ~$376 to acquire), it makes the capitalized asset
cost correct, and it guarantees the receipt tally balances with no residual.

The alternative — booking shipping as its own expense line — is offered as a
per-receipt toggle for users who prefer it, but is not the default because it
skews per-project cost.

Bank FX fees are **not** modelled separately: they are already inside the
derived rate, which is why the derived rate is the right one.

---

## 5. UI

The entry flow follows the order things actually happen to the user, and the
words on screen are the words on their paperwork ("charge", "receipt", "item")
— never "payment entity" or "line item".

**Charge screen (new).** Record the debit exactly as the statement shows it:
amount, date, card label, reference. Upload the statement excerpt. Then add
receipts under it. A persistent running tally is the centrepiece:

> **SAR 1,500.00 of SAR 4,631.10 accounted for — SAR 3,131.10 remaining**

Green when balanced. That single line is what makes the feature self-teaching:
the user always knows whether a charge is fully explained, and so does the
auditor.

**Receipt panel.** Vendor, order/receipt number, date, currency, subtotal,
shipping, tax, total. Drag-drop the receipt PDF. Its own item tally with the
same green/amber treatment.

**Items.** Each item row: description, category, **project**, amount,
quantity, and an asset picker. The asset picker is multi-select and searchable,
with **"Create asset from this line"** carrying across name, cost, vendor, date
and receipt. `quantity: 4` offers to create four assets and split the cost
evenly.

**Asset detail.** The existing project field is relabelled **Funding project**
(read-only once expenses reference it) and gains a **Used by** panel — add a
project with dates. The panel carries the explicit caption *"Usage does not
move any cost between projects."*

**Reconciliation screen (new).** All charges, filterable by state, with the
variance shown. The lab's get-audit-ready list.

**Existing screens.** `ExpenseFormModal` gains the multi-asset picker and an
optional "attach to a receipt" step; used standalone it behaves exactly as it
does today, so nothing the user already knows breaks.

---

## 6. PDFs

The machinery already exists and is proven: `apps.projects.report` merges
arbitrary PDFs with fitz, converts uploaded images to full pages via Pillow,
inserts labelled divider pages, and runs as a Celery job behind the `apps.jobs`
poller. M8's packs are assembly, not new plumbing.

### 6.1 Expense pack (the user's explicit request)
One builder, invoked at any of three levels:

- **From a charge** — cover sheet (amount, date, card, reference, FX rate
  used) → bank statement excerpt → per receipt: divider, receipt scan, item
  table, each item's asset photos → closing summary of how the charge splits
  across projects.
- **From a receipt** — the same, narrowed to that receipt, still including the
  statement page.
- **From one item** — a compact single-item proof: item, cost in both
  currencies, its receipt, its charge, its photos and assets.

Every page footers the charge reference and page number, so a page separated
from the stack is still traceable.

### 6.2 Project report additions
The M7 report gains, and reshapes:

3. Itemized ledger — **grouped by receipt**, new `Charge ref`, `Receipt #`,
   `Asset(s)` columns, amounts in both currencies.
4. **Reconciliation appendix (new)** — per charge touching this project:
   > **Bank charge — 2 Mar 2026 — SAR 4,631.10 — Visa •4321**
   > Receipt 1 · Amazon 123-4567890 · USD 400.00 (SAR 1,500.00 @ 3.750000) → *this project: SAR 1,500.00* → scan, p. 14
   > Receipt 2 · Amazon 123-4567890 · USD 834.56 (SAR 3,129.60) → *this project: SAR 0.00*
   > **This project's share: SAR 1,500.00.** Remainder covers other projects.

   Amounts only for other projects — never their names or detail (§7).
5. Assets — split into *funded by this project* / *used by this project,
   funded elsewhere (no cost to this project)* / *funded here, used elsewhere*.
6. Scan appendix — each receipt once, plus the statement excerpt.

### 6.3 Audit-readiness checklist (new, recommended alongside 6.1)
Renders the reconciliation state as a to-do list: charges with money
unaccounted for, expenses with no receipt, receipts whose items don't sum,
assets with no photo or serial. Cheap — the tallies already exist — and it is
the artifact that finds problems *before* the auditor does.

### 6.4 Cross-cutting PDF polish
Built once in `apps.projects.report` (or lifted to `apps.common`), applied to
every pack: **PDF bookmarks + clickable table of contents** (fitz `set_toc`),
**sequential page stamping** so the ledger's "receipt at p. 47" resolves,
**DRAFT/FINAL watermark**, and a **provenance footer** (generated by, when,
app version). Email delivery reuses the existing `EmailProvider`.

### 6.5 Nice-to-have packs (Phase 4, user picks)
Asset dossier · period statement · vendor statement · inventory count sheet ·
custody handover form. (An insurance/equipment schedule was considered and
**dropped at the user's request** — do not reintroduce it.) Each is a new
`ReportData` dataclass + template against machinery that already exists.

---

## 7. RBAC & tenant isolation — the R4 risk in M8

**User directive: a ProjectLead has complete permission over everything
relating to a project they lead** — its expenses, receipts, charges, assets,
documents and reports. M8 must not make a lead ask an Admin to reconcile their
own paperwork.

The complication is that `Payment` and `Purchase` **span projects by
construction** — a single Amazon charge can pay for two grants at once — so
"project-related" is not a clean partition for them. Resolution:

- New keys `finance.payment.view`, `finance.payment.manage`, granted to
  **Admin tenant-wide** and to **ProjectLead project-scoped**, via the existing
  union-of-memberships scope rule (`rbac.md` §1/§3) — the same 🟡 shape as
  `expense.*`.
- A lead's **visible set of charges** = every charge carrying at least one
  expense line booked to a project they lead. Charges touching only other
  people's projects are invisible.
- On a visible charge the lead gets: the full charge header (amount, date,
  card label, statement reference), the statement scan, every receipt under it,
  and **full read/write on the lines belonging to their own projects**.
- Lines belonging to projects they do **not** lead are shown as a single
  **unnamed aggregate** — `Other projects: SAR 3,131.10` — never itemized, never
  naming the project. Same treatment in the report's reconciliation appendix.
- **Header edits require full coverage**: a lead may edit or delete a charge's
  own fields only if *every* line on it belongs to projects they lead.
  Otherwise the header is read-only for them (Admin edits it) while their lines
  stay fully editable. This keeps two leads from overwriting each other's
  shared charge.
- Creating charges and receipts: any lead may, and becomes its creator.
- `finance.*` endpoints filter tenant-first, then apply the visible-set rule
  above. RLS on `finance_payment` / `finance_purchase` is **tenant-only** (no
  project predicate — a payment has no single project), so the project-level
  constraint lives in the queryset + permission layer and must be covered by
  explicit tests. Documented in the migration, since it is the one M8 table
  pair whose RLS is weaker than its RBAC.

> **Accepted consequence, flagged for the user (§10.5):** the charge *header
> total* is a shared fact — SAR 4,631.10 was one debit. A lead who can
> reconcile at all can see that total, and can therefore infer that the
> remainder went somewhere else. What they can never see is which project, or
> what was bought. Fully hiding the total would make reconciliation impossible,
> which is the whole point of the feature.

Audit keys added to `rbac.md` §5: `payment.create/update/delete`,
`purchase.create/update/delete`, `expense.asset_link.*`, `asset.usage.*`.

---

## 8. Migration & backfill safety
- All new FKs nullable; all new columns defaulted. No M7 row is invalidated.
- One backfill migration: `Expense.asset` → `ExpenseAssetLink`. Reversible
  (drop the link rows), verified up **and** down against a scratch Postgres per
  the `add-migration` skill.
- No destructive operation on the dev or NAS database (standing rule in
  memory: forward-only, confirm first).
- `Expense.asset` removal is a **separate, later** migration one release after
  the deprecation — not in the same release as the backfill.

## 9. Phases

Each phase is independently shippable and independently useful.

**Phase 1 — Links.** `ExpenseAssetLink` + `AssetProjectUsage`, backfill, API,
multi-asset picker in `ExpenseFormModal`, "Used by" panel on asset detail,
report asset-section split.
*Agents:* db-migration-specialist → backend-engineer → frontend-engineer → qa
→ code-reviewer.
*Exit:* an expense links to N assets; an asset is used by N projects; usage
provably moves no money (test asserts project spend unchanged after adding a
usage row); existing single-asset expenses render unchanged.

**Phase 2 — Money model.** `apps/finance` with `Payment`/`Purchase` + RLS,
`Expense.purchase`/`allocated_overhead`/`amount_settled`, FX derivation,
largest-remainder allocation, reconciliation tallies, new permission keys,
charge/receipt/items UI, reconciliation screen.
*Exit:* the Amazon scenario end-to-end — one SAR charge, three USD receipts,
five items across two projects — reconciles to zero variance; a ProjectLead
reconciles that shared charge end-to-end with no Admin help, while an
RBAC-matrix test proves they cannot read the other project's line items, name
or per-line amounts and cannot edit a charge header they do not fully cover;
tenant-isolation tests still hold; rounding test proves splits sum exactly.

**Phase 3 — PDFs.** Expense pack (§6.1), report additions (§6.2),
audit-readiness checklist (§6.3), cross-cutting polish (§6.4).
*Exit:* a generated pack contains statement + every receipt + item tables +
photos, in order, with a working TOC and resolvable page references; renders in
Celery; a regenerated report reproduces byte-identical figures.

**Phase 4 — Nice-to-have packs** (§6.5), user picks which.

Versioning: this is a milestone, so it lands as **0.16.0** per the
minor-tracks-milestones rule; individual phases accumulate under
`## [Unreleased]` in `CHANGELOG.md` and only the final phase promotes a version.

## 10. Assumptions & open questions
Chosen defaults, flagged per CLAUDE.md's workflow rule:

1. **Shipping/tax pro-rata by amount** is the default, with a per-receipt
   toggle for a separate shipping line. *(assumed)*
2. **FX rate derived from the debit**, no external rate source. *(assumed)*
3. **Co-funding a single line across projects is out of scope.** The extension
   point is an `ExpenseAllocation(expense, project, amount)` table; the model
   above does not preclude it. *(assumed — confirm if any grant requires cost
   share)*
4. **Refunds, returns and partial credits are out of scope** for M8.
   *(decided by the user, after reviewing a full design: deferred deliberately,
   not overlooked.)* The intended shape when it is picked up: a `Refund` child
   of the order carrying its own amount/date/statement-ref/credit-note, with
   optional links to the items returned; returned items are marked rather than
   deleted; the reconciliation invariant becomes `items - refunds = net paid`.
   Phase 3's reports assume the no-refund shape, so adding it later means
   revisiting the pack and the report's reconciliation section.
5. **ProjectLead has full permission on everything project-related**, charges
   included, scoped to charges that touch a project they lead (§7).
   *(decided by the user — resolved)* The one residual: a lead can see the
   **total** of a shared charge, though never the other project's identity or
   items. Unavoidable if leads are to reconcile at all. *(flagged, accepted)*
6. Statement excerpts contain account data; storage is the existing backend
   with no extra encryption. *(assumed — same posture as invoice scans today)*
