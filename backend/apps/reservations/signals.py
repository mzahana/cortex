"""Domain events for the reservation lifecycle (T3.2, `docs/architecture.md`
§6: "business logic emits domain events ... it never imports Brevo
directly"). Same in-process `django.dispatch.Signal` mechanism, sent from
`transaction.on_commit`, as `apps.stock.signals.low_stock_crossed` (see that
module's docstring for the full "why a Signal, not an outbox table"
reasoning) -- kept identical here for M5 to wire a `@receiver` onto without
having to learn a second convention.

## The three events (`docs/tasks/M3-reservations-checkout.md` T3.2)

- `approval_request` -- sent when a NEW reservation is created in `pending`
  status (`Category.requires_approval` is true for the asset's category).
  M5 uses this to notify whoever holds `reservation.approve` in scope
  (Admin tenant-wide, or the ProjectLead of the asset's project).
- `approval_decision` -- sent whenever a `pending` reservation is decided
  (`approved=True`/`False`), i.e. the approve/reject actions. M5 uses this to
  notify the original requester of the outcome.
- `reservation_confirmed` -- sent whenever a reservation BECOMES `approved`,
  whether that happened immediately on create (self-service,
  `requires_approval=False`) or via the explicit approve action later. M5
  uses this to notify the requester their booking is confirmed. Note an
  `approval_decision(approved=True)` and a `reservation_confirmed` are both
  sent for the "approve a pending reservation" path -- they are two distinct
  facts (a decision was made; a booking is now live) that different M5
  receivers may care about independently.

All three are sent via `send_robust` (a receiver raising never breaks the
request/service call that triggered it) and only from
`transaction.on_commit` (never fire for a change that gets rolled back, e.g.
the conflict/limit guards raising mid-transaction; by the time any of these
fire, the row is already durably committed, so a receiver that re-reads it
sees consistent state).

## Common kwargs (every event, `sender=Reservation`)

| kwarg            | type       | meaning                                    |
|------------------|------------|---------------------------------------------|
| `reservation_id` | `int`      | the `Reservation.id`                        |
| `tenant_id`      | `int`      | tenant, for scoping the notification         |
| `asset_id`       | `int`      | the durable `Asset.id` being reserved        |
| `user_id`        | `int`      | the requester/holder                        |
| `start_at`       | `datetime` | reservation window start                     |
| `end_at`         | `datetime` | reservation window end                       |
| `occurred_at`    | `datetime` | wall-clock time the signal was sent (post-commit) |

`approval_decision` additionally carries `approved` (`bool`), `approver_id`
(`int`), and `note` (`str`, the approver's `approval_note`).
"""

from __future__ import annotations

import django.dispatch

approval_request = django.dispatch.Signal()
approval_decision = django.dispatch.Signal()
reservation_confirmed = django.dispatch.Signal()
