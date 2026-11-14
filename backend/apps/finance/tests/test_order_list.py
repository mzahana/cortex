"""The order list: paging and `?search=` (project hub Expenses tab).

Regression guards:
- the list used to fall back to DRF's global pager, which ignores `?page_size=`,
  so a project never showed more than 25 orders;
- a searched list must keep each order's REAL number (the unsearched
  `RowNumber()` window would renumber matches 1..k).
"""

from __future__ import annotations

import datetime
from decimal import Decimal

import pytest

from apps.common.tests.factories import (
    DEFAULT_TEST_PASSWORD,
    ExpenseFactory,
    ProjectFactory,
    TenantFactory,
    UserFactory,
    upgrade_tenant_wide_role,
)
from apps.finance.models import Payment, Purchase
from apps.rbac.permission_keys import ROLE_ADMIN
from apps.tenancy.context import tenant_context

pytestmark = pytest.mark.django_db

BASE = datetime.date(2026, 1, 1)


def _login(client, tenant, user):
    response = client.post(
        "/api/v1/auth/login",
        {"tenant": tenant.slug, "email": user.email, "password": DEFAULT_TEST_PASSWORD},
        content_type="application/json",
    )
    assert response.status_code == 200, response.content


def _order(tenant, project, day, **kwargs):
    kwargs.setdefault("vendor", f"Vendor{day}")
    kwargs.setdefault("amount", Decimal("100.00"))
    return Payment.all_objects.create(
        tenant=tenant,
        project=project,
        paid_on=BASE + datetime.timedelta(days=day),
        currency="SAR",
        **kwargs,
    )


def _receipt(tenant, order, **kwargs):
    return Purchase.all_objects.create(
        tenant=tenant,
        payment=order,
        date=order.paid_on,
        currency="SAR",
        total=Decimal("10.00"),
        **kwargs,
    )


def _item(tenant, project, purchase, description, **kwargs):
    return ExpenseFactory(
        tenant=tenant, project=project, purchase=purchase, description=description, **kwargs
    )


@pytest.fixture
def setup(client):
    tenant = TenantFactory()
    with tenant_context(tenant.id):
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        project = ProjectFactory(tenant=tenant)
    _login(client, tenant, admin)
    return tenant, project


def _list(client, project, **params):
    query = "&".join(f"{k}={v}" for k, v in params.items())
    response = client.get(f"/api/v1/orders/?project={project.id}&{query}")
    assert response.status_code == 200, response.content
    return response.json()


def _ids(body):
    return [row["id"] for row in body["results"]]


# --- 1. paging ---------------------------------------------------------------


def test_page_size_100_returns_every_order_on_one_page(client, setup):
    tenant, project = setup
    with tenant_context(tenant.id):
        orders = [_order(tenant, project, d) for d in range(40)]

    body = _list(client, project, page_size=100)
    assert body["count"] == 40
    assert body["next"] is None
    assert sorted(_ids(body)) == sorted(o.id for o in orders)


def test_default_page_is_25_and_has_a_next_link(client, setup):
    tenant, project = setup
    with tenant_context(tenant.id):
        for d in range(30):
            _order(tenant, project, d)
    body = _list(client, project)
    assert body["count"] == 30
    assert len(body["results"]) == 25
    assert body["next"] is not None


def test_page_size_above_100_is_clamped(client, setup):
    tenant, project = setup
    with tenant_context(tenant.id):
        Payment.all_objects.bulk_create(
            [
                Payment(
                    tenant=tenant,
                    project=project,
                    vendor=f"V{d}",
                    amount=Decimal("1.00"),
                    currency="SAR",
                    paid_on=BASE + datetime.timedelta(days=d),
                )
                for d in range(105)
            ]
        )
    body = _list(client, project, page_size=500)
    assert body["count"] == 105
    assert len(body["results"]) == 100
    assert body["next"] is not None


def test_page_two_continues_without_overlap(client, setup):
    tenant, project = setup
    with tenant_context(tenant.id):
        for d in range(30):
            _order(tenant, project, d)
    first = _list(client, project, page_size=20, page=1)
    second = _list(client, project, page_size=20, page=2)
    assert len(first["results"]) == 20 and len(second["results"]) == 10
    assert not set(_ids(first)) & set(_ids(second))
    assert second["next"] is None


# --- 2. search matches -------------------------------------------------------


def test_search_matches_order_vendor_case_insensitively(client, setup):
    tenant, project = setup
    with tenant_context(tenant.id):
        hit = _order(tenant, project, 1, vendor="Acme Robotics")
        _order(tenant, project, 2, vendor="Other")
    assert _ids(_list(client, project, search="acme ROBOTICS")) == [hit.id]


def test_search_matches_notes_and_statement_ref(client, setup):
    tenant, project = setup
    with tenant_context(tenant.id):
        by_notes = _order(tenant, project, 1, notes="rush shipping for demo")
        by_ref = _order(tenant, project, 2, statement_ref="STMT-77AB")
        _order(tenant, project, 3)
    assert _ids(_list(client, project, search="RUSH")) == [by_notes.id]
    assert _ids(_list(client, project, search="stmt-77")) == [by_ref.id]


def test_search_matches_receipt_fields(client, setup):
    tenant, project = setup
    with tenant_context(tenant.id):
        by_receipt = _order(tenant, project, 1)
        _receipt(tenant, by_receipt, receipt_number="RCPT-9981")
        by_vendor_order = _order(tenant, project, 2)
        _receipt(tenant, by_vendor_order, vendor_order_number="113-5550001-ZZ")
        by_receipt_vendor = _order(tenant, project, 3)
        _receipt(tenant, by_receipt_vendor, vendor="Shenzhen Parts Ltd")
        _order(tenant, project, 4)
    assert _ids(_list(client, project, search="rcpt-9981")) == [by_receipt.id]
    assert _ids(_list(client, project, search="5550001")) == [by_vendor_order.id]
    assert _ids(_list(client, project, search="shenzhen")) == [by_receipt_vendor.id]


def test_search_matches_item_description_once_even_with_many_matching_items(client, setup):
    tenant, project = setup
    with tenant_context(tenant.id):
        order = _order(tenant, project, 1)
        r1 = _receipt(tenant, order)
        r2 = _receipt(tenant, order)
        _item(tenant, project, r1, "Jetson Orin module")
        _item(tenant, project, r1, "Jetson carrier board")
        _item(tenant, project, r2, "JETSON heatsink")
        _order(tenant, project, 2)
    body = _list(client, project, search="jetson")
    assert _ids(body) == [order.id]
    assert body["count"] == 1


def test_search_with_no_match_is_empty(client, setup):
    tenant, project = setup
    with tenant_context(tenant.id):
        _order(tenant, project, 1)
    body = _list(client, project, search="zzz-nothing")
    assert body["count"] == 0 and body["results"] == []


# --- 3. order number ---------------------------------------------------------


def test_numeric_search_matches_order_number_and_keeps_the_real_number(client, setup):
    tenant, project = setup
    with tenant_context(tenant.id):
        orders = [_order(tenant, project, d, vendor=f"Vendor-{d}") for d in range(1, 9)]
    real = {row["id"]: row["number"] for row in _list(client, project, page_size=100)["results"]}
    assert sorted(real.values()) == list(range(1, 9))

    for term in ("#7", "7", "# 7"):
        body = _list(client, project, search=term.replace("#", "%23").replace(" ", "%20"))
        assert _ids(body) == [orders[6].id], term
        assert body["results"][0]["number"] == 7 == real[orders[6].id]


def test_searched_results_are_not_renumbered(client, setup):
    """The key regression: matches 3 and 6 must stay Order 3 and Order 6, not
    become 1 and 2."""
    tenant, project = setup
    with tenant_context(tenant.id):
        orders = []
        for d in range(1, 9):
            vendor = "Widgets" if d in (3, 6) else f"Plain-{d}"
            orders.append(_order(tenant, project, d, vendor=vendor))
    real = {row["id"]: row["number"] for row in _list(client, project, page_size=100)["results"]}
    body = _list(client, project, search="widgets")
    assert body["count"] == 2
    got = {row["id"]: row["number"] for row in body["results"]}
    assert got == {orders[2].id: 3, orders[5].id: 6}
    assert all(real[i] == n for i, n in got.items())


def test_number_ties_on_the_same_day_break_by_id_when_searched(client, setup):
    tenant, project = setup
    with tenant_context(tenant.id):
        a = _order(tenant, project, 1, vendor="Same Day A")
        b = _order(tenant, project, 1, vendor="Same Day B")
    body = _list(client, project, search="same%20day")
    assert {r["id"]: r["number"] for r in body["results"]} == {a.id: 1, b.id: 2}


def test_numbers_are_per_project_when_searched(client, setup):
    tenant, project = setup
    with tenant_context(tenant.id):
        other = ProjectFactory(tenant=tenant)
        _order(tenant, other, 0, vendor="Noise")
        _order(tenant, other, 1, vendor="Noise")
        mine = _order(tenant, project, 5, vendor="Mine")
    body = _list(client, project, search="mine")
    assert _ids(body) == [mine.id]
    assert body["results"][0]["number"] == 1


# --- 4. amount search --------------------------------------------------------


def test_amount_search_matches_with_and_without_thousands_separator(client, setup):
    tenant, project = setup
    with tenant_context(tenant.id):
        hit = _order(tenant, project, 1, amount=Decimal("4631.10"))
        _order(tenant, project, 2, amount=Decimal("12.00"))
    assert _ids(_list(client, project, search="4631.10")) == [hit.id]
    assert _ids(_list(client, project, search="4,631.10")) == [hit.id]
    assert _ids(_list(client, project, search="4%2C631.10")) == [hit.id]


@pytest.mark.parametrize(
    "term",
    [
        "1e999999",
        "NaN",
        "-NaN",
        "sNaN",
        "Infinity",
        "-Infinity",
        "1e12",
        "99999999999999999999",
        ".",
    ]
    + ["%25", "1e-999999", "0x10", "%27%3B--"],
)
def test_hostile_search_terms_never_500(client, setup, term):
    tenant, project = setup
    with tenant_context(tenant.id):
        _order(tenant, project, 1)
    response = client.get(f"/api/v1/orders/?project={project.id}&search={term}")
    assert response.status_code == 200, response.content


def test_huge_number_search_does_not_overflow(client, setup):
    """`#<huge int>` must not blow up the integer comparison either."""
    tenant, project = setup
    with tenant_context(tenant.id):
        _order(tenant, project, 1)
    response = client.get(f"/api/v1/orders/?project={project.id}&search=%2399999999999999999999")
    assert response.status_code == 200, response.content
    assert response.json()["count"] == 0


# --- 5. tenant / project isolation ------------------------------------------


def test_search_never_returns_another_tenants_orders(client, setup):
    tenant, project = setup
    other_tenant = TenantFactory()
    with tenant_context(other_tenant.id):
        other_project = ProjectFactory(tenant=other_tenant)
        foreign = _order(other_tenant, other_project, 1, vendor="SharedVendor", amount="55.55")
        foreign_receipt = _receipt(other_tenant, foreign, receipt_number="SHARED-RCPT")
        _item(other_tenant, other_project, foreign_receipt, "SharedVendor item")
    with tenant_context(tenant.id):
        mine = _order(tenant, project, 1, vendor="SharedVendor")

    for term in ("sharedvendor", "SHARED-RCPT", "55.55", "%231"):
        body = _list(client, project, search=term)
        assert foreign.id not in _ids(body), term
    assert _ids(_list(client, project, search="sharedvendor")) == [mine.id]

    # Even naming the foreign project directly yields nothing.
    response = client.get(f"/api/v1/orders/?project={other_project.id}&search=sharedvendor")
    assert response.status_code == 200
    assert response.json()["count"] == 0
    # ...and without `?project=` at all.
    response = client.get("/api/v1/orders/?search=sharedvendor")
    assert foreign.id not in [r["id"] for r in response.json()["results"]]


def test_search_on_one_project_never_returns_another_projects_orders(client, setup):
    tenant, project = setup
    with tenant_context(tenant.id):
        other = ProjectFactory(tenant=tenant)
        _order(tenant, other, 1, vendor="Acme")
        _item(tenant, other, _receipt(tenant, _order(tenant, other, 2), notes="x"), "Acme thing")
        mine = _order(tenant, project, 1, vendor="Acme")
    assert _ids(_list(client, project, search="acme")) == [mine.id]


def test_search_ignored_for_detail_routes_and_detail_number_is_real(client, setup):
    tenant, project = setup
    with tenant_context(tenant.id):
        orders = [_order(tenant, project, d, vendor=f"V{d}") for d in range(3)]
    response = client.get(f"/api/v1/orders/{orders[2].id}/?search=nomatch")
    assert response.status_code == 200
    assert response.json()["number"] == 3
    assert client.get(f"/api/v1/orders/{orders[0].id}/").json()["number"] == 1


def test_patch_response_carries_the_real_number(client, setup):
    tenant, project = setup
    with tenant_context(tenant.id):
        orders = [_order(tenant, project, d, vendor=f"V{d}") for d in range(3)]
    response = client.patch(
        f"/api/v1/orders/{orders[2].id}/",
        {"notes": "edited"},
        content_type="application/json",
    )
    assert response.status_code == 200, response.content
    assert response.json()["number"] == 3


def test_legacy_orders_without_a_project_number_like_the_window(client, setup):
    """project=NULL rows are partitioned together by the window; the searched
    and detail numbering must agree with the unsearched list."""
    tenant, project = setup
    with tenant_context(tenant.id):
        legacy = [_order(tenant, None, d, vendor=f"Legacy-{d}") for d in range(4)]
        _order(tenant, project, 0, vendor="Proj")
    unsearched = {
        r["id"]: r["number"] for r in client.get("/api/v1/orders/?page_size=100").json()["results"]
    }
    assert sorted(unsearched[o.id] for o in legacy) == [1, 2, 3, 4]
    body = client.get("/api/v1/orders/?search=legacy").json()
    assert {r["id"]: r["number"] for r in body["results"]} == {
        o.id: unsearched[o.id] for o in legacy
    }
    body = client.get("/api/v1/orders/?search=%233").json()
    assert [r["id"] for r in body["results"]] == [legacy[2].id]
    assert client.get(f"/api/v1/orders/{legacy[3].id}/").json()["number"] == 4


def test_nul_in_search_is_not_a_500(client, setup):
    tenant, project = setup
    with tenant_context(tenant.id):
        _order(tenant, project, 1, vendor="Acme")
    for term in ("%00", "ac%00me"):
        response = client.get(f"/api/v1/orders/?project={project.id}&search={term}")
        # A NUL is rejected by a framework layer before the view runs (400,
        # "Null characters are not allowed."); the contract is: never a 500.
        assert response.status_code in (200, 400), (term, response.content)


@pytest.mark.parametrize("value", ["abc", "1.5", "-1", "%00", "1;2"])
def test_non_numeric_project_param_is_empty_not_500(client, setup, value):
    tenant, project = setup
    with tenant_context(tenant.id):
        _order(tenant, project, 1)
    response = client.get(f"/api/v1/orders/?project={value}")
    assert response.status_code == 200, response.content
    assert response.json()["results"] == []


# --- 7. RBAC: project-scoped lead --------------------------------------------


@pytest.fixture
def lead_setup(client):
    """A lead of project A only (no tenant-wide membership), plus an admin
    view of the same tenant; project B holds an order the lead must not see."""
    from apps.common.tests.factories import add_project_membership
    from apps.rbac.models import Membership
    from apps.rbac.permission_keys import ROLE_PROJECT_LEAD

    tenant = TenantFactory()
    with tenant_context(tenant.id):
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        lead = UserFactory(tenant=tenant)
        Membership.all_objects.filter(user=lead, project__isnull=True).delete()
        a = ProjectFactory(tenant=tenant)
        b = ProjectFactory(tenant=tenant)
        add_project_membership(lead, a, ROLE_PROJECT_LEAD)
        order_a = _order(tenant, a, 1, vendor="AlphaCo")
        order_b = _order(tenant, b, 1, vendor="BetaSecret")
        _item(tenant, b, _receipt(tenant, order_b, receipt_number="B-RCPT"), "secret item")
    return tenant, admin, lead, a, b, order_a, order_b


def test_lead_cannot_list_or_search_another_projects_orders(client, lead_setup):
    tenant, _admin, lead, a, b, order_a, order_b = lead_setup
    _login(client, tenant, lead)

    own = client.get(f"/api/v1/orders/?project={a.id}")
    assert own.status_code == 200 and [r["id"] for r in own.json()["results"]] == [order_a.id]

    for url in (
        f"/api/v1/orders/?project={b.id}",
        f"/api/v1/orders/?project={b.id}&search=BetaSecret",
        "/api/v1/orders/?search=BetaSecret",
        "/api/v1/orders/?search=B-RCPT",
        "/api/v1/orders/?search=secret%20item",
    ):
        response = client.get(url)
        assert response.status_code == 200, url
        assert response.json()["count"] == 0, url
    unscoped = client.get("/api/v1/orders/?page_size=100").json()
    assert [r["id"] for r in unscoped["results"]] == [order_a.id]


def test_lead_gets_404_on_another_projects_order_detail(client, lead_setup):
    tenant, _admin, lead, _a, _b, _order_a, order_b = lead_setup
    _login(client, tenant, lead)
    url = f"/api/v1/orders/{order_b.id}/"
    assert client.get(url).status_code == 404
    assert client.patch(url, {"notes": "x"}, content_type="application/json").status_code == 404
    assert client.delete(url).status_code == 404
    with tenant_context(tenant.id):
        assert Payment.objects.filter(pk=order_b.pk, notes="").exists()


def test_admin_still_sees_every_project(client, lead_setup):
    tenant, admin, _lead, _a, _b, order_a, order_b = lead_setup
    _login(client, tenant, admin)
    body = client.get("/api/v1/orders/?page_size=100").json()
    assert {r["id"] for r in body["results"]} == {order_a.id, order_b.id}
    assert [r["id"] for r in client.get("/api/v1/orders/?search=BetaSecret").json()["results"]] == [
        order_b.id
    ]


def _payload(project, **extra):
    return {
        "project": project.id,
        "vendor": "New",
        "paid_on": "2026-03-02",
        "amount": "10.00",
        "currency": "SAR",
        **extra,
    }


def test_lead_cannot_create_an_order_in_another_project(client, lead_setup):
    tenant, _admin, lead, a, b, _oa, _ob = lead_setup
    _login(client, tenant, lead)
    bad = client.post("/api/v1/orders/", _payload(b), content_type="application/json")
    assert bad.status_code == 400, bad.content
    assert "project" in bad.json() or "project" in str(bad.json())
    with tenant_context(tenant.id):
        assert not Payment.objects.filter(project=b, vendor="New").exists()
    ok = client.post("/api/v1/orders/", _payload(a), content_type="application/json")
    assert ok.status_code == 201, ok.content


def test_lead_cannot_move_their_order_to_another_project(client, lead_setup):
    tenant, _admin, lead, a, b, order_a, _ob = lead_setup
    _login(client, tenant, lead)
    response = client.patch(
        f"/api/v1/orders/{order_a.id}/", {"project": b.id}, content_type="application/json"
    )
    assert response.status_code == 400, response.content
    with tenant_context(tenant.id):
        assert Payment.objects.get(pk=order_a.pk).project_id == a.id


# --- 6. query budget ---------------------------------------------------------


def _seed_matching(tenant, project, n):
    with tenant_context(tenant.id):
        for d in range(n):
            order = _order(tenant, project, 100 + d, vendor="Budget Co")
            for _ in range(2):
                receipt = _receipt(tenant, order, receipt_number=f"R{d}")
                _item(tenant, project, receipt, "budget item")
                _item(tenant, project, receipt, "budget item 2")


def _count_queries(client, project, term, context):
    with context() as ctx:
        response = client.get(f"/api/v1/orders/?project={project.id}&search={term}")
    assert response.status_code == 200
    return len(ctx.captured_queries), response.json()["count"]


def test_searched_list_query_count_does_not_scale_with_results(client, setup):
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    tenant, project = setup
    _seed_matching(tenant, project, 3)
    ctx = lambda: CaptureQueriesContext(connection)  # noqa: E731
    _count_queries(client, project, "budget", ctx)  # warm caches (session, perms)
    few, few_n = _count_queries(client, project, "budget", ctx)

    _seed_matching(tenant, project, 7)
    many, many_n = _count_queries(client, project, "budget", ctx)

    assert (few_n, many_n) == (3, 10)
    assert many == few, f"query count scales with results: {few} -> {many}"
    assert many <= 25  # absolute ceiling; currently 18 (auth+RBAC+list+prefetches)


# --- 8. writes are decided by the order's own project ------------------------


def test_view_only_on_b_cannot_write_b_even_when_the_order_is_empty(client):
    """payment.view on B + payment.manage only on A: B's order (which has no
    items, so a "covers every line" rule would vacuously pass) is readable but
    not PATCH/DELETE-able, while A's own order stays editable/deletable."""
    from apps.common.tests.factories import add_project_membership
    from apps.rbac.models import Membership, Permission, Role, RolePermission
    from apps.rbac.permission_keys import PAYMENT_VIEW, ROLE_PROJECT_LEAD

    tenant = TenantFactory()
    with tenant_context(tenant.id):
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        user = UserFactory(tenant=tenant)
        Membership.all_objects.filter(user=user, project__isnull=True).delete()
        a = ProjectFactory(tenant=tenant)
        b = ProjectFactory(tenant=tenant)
        viewer = Role.all_objects.create(tenant=tenant, key="finance-viewer", name="Finance viewer")
        RolePermission.all_objects.create(
            tenant=tenant, role=viewer, permission=Permission.objects.get(key=PAYMENT_VIEW)
        )
        Membership.all_objects.create(tenant=tenant, user=user, role=viewer, project=b)
        add_project_membership(user, a, ROLE_PROJECT_LEAD)
        order_b = _order(tenant, b, 1, vendor="B-empty")
        order_a1 = _order(tenant, a, 1, vendor="A-one")
        order_a2 = _order(tenant, a, 2, vendor="A-two")
    _login(client, tenant, user)

    url_b = f"/api/v1/orders/{order_b.id}/"
    assert client.get(url_b).status_code == 200
    assert client.patch(url_b, {"notes": "x"}, content_type="application/json").status_code == 403
    assert client.delete(url_b).status_code == 403
    with tenant_context(tenant.id):
        assert Payment.objects.filter(pk=order_b.pk, notes="").exists()

    patched = client.patch(
        f"/api/v1/orders/{order_a1.id}/", {"notes": "mine"}, content_type="application/json"
    )
    assert patched.status_code == 200, patched.content
    assert client.delete(f"/api/v1/orders/{order_a2.id}/").status_code == 204

    # Admin is unaffected.
    client.post("/api/v1/auth/logout", content_type="application/json")
    _login(client, tenant, admin)
    assert (
        client.patch(url_b, {"notes": "admin"}, content_type="application/json").status_code == 200
    )
    assert client.delete(url_b).status_code == 204


# --- 9. payment-date range (`?date_from=` / `?date_to=`) ----------------------
#
# BASE is 2026-01-01, so `_order(..., day=N)` is paid on 2026-01-(N+1).


@pytest.fixture
def dated(setup):
    """Five orders paid Jan 1..5 (numbers 1..5), plus one in another project
    paid inside every range below."""
    tenant, project = setup
    with tenant_context(tenant.id):
        orders = [_order(tenant, project, d, vendor=f"Shop{d}") for d in range(5)]
        other = ProjectFactory(tenant=tenant)
        _order(tenant, other, 2, vendor="Shop2")
    return tenant, project, orders


def test_date_range_is_inclusive_on_both_ends(client, dated):
    _tenant, project, orders = dated
    body = _list(client, project, date_from="2026-01-02", date_to="2026-01-04")
    assert sorted(_ids(body)) == sorted(o.id for o in orders[1:4])
    assert body["count"] == 3


def test_date_from_alone_is_open_ended(client, dated):
    _tenant, project, orders = dated
    body = _list(client, project, date_from="2026-01-04")
    assert sorted(_ids(body)) == sorted(o.id for o in orders[3:])


def test_date_to_alone_is_open_ended(client, dated):
    _tenant, project, orders = dated
    body = _list(client, project, date_to="2026-01-02")
    assert sorted(_ids(body)) == sorted(o.id for o in orders[:2])


def test_single_day_range(client, dated):
    _tenant, project, orders = dated
    body = _list(client, project, date_from="2026-01-03", date_to="2026-01-03")
    assert _ids(body) == [orders[2].id]


def test_inverted_range_is_empty_not_an_error(client, dated):
    _tenant, project, _orders = dated
    body = _list(client, project, date_from="2026-01-05", date_to="2026-01-01")
    assert body["count"] == 0


def test_date_filtered_results_keep_their_real_numbers(client, dated):
    """The key regression guard: a date filter narrows rows WITHIN a project,
    so the window numbering must not renumber the survivors from 1."""
    _tenant, project, orders = dated
    body = _list(client, project, date_from="2026-01-04")
    numbers = {row["id"]: row["number"] for row in body["results"]}
    assert numbers == {orders[3].id: 4, orders[4].id: 5}


def test_date_range_combines_with_search(client, dated):
    tenant, project, orders = dated
    with tenant_context(tenant.id):
        _order(tenant, project, 2, vendor="Amazon")  # Jan 3
        _order(tenant, project, 9, vendor="Amazon")  # Jan 10, out of range
    body = _list(client, project, search="amazon", date_from="2026-01-01", date_to="2026-01-05")
    assert [row["vendor"] for row in body["results"]] == ["Amazon"]
    assert body["results"][0]["paid_on"] == "2026-01-03"


def test_date_range_never_leaks_another_projects_orders(client, dated):
    _tenant, project, orders = dated
    body = _list(client, project, date_from="2026-01-01", date_to="2026-01-31")
    assert sorted(_ids(body)) == sorted(o.id for o in orders)


def test_date_range_pages_with_correct_count(client, setup):
    tenant, project = setup
    with tenant_context(tenant.id):
        for d in range(40):
            _order(tenant, project, d)  # Jan 1 .. Feb 9
    body = _list(client, project, date_from="2026-01-01", date_to="2026-01-31", page_size=25)
    assert body["count"] == 31
    assert len(body["results"]) == 25 and body["next"] is not None


@pytest.mark.parametrize(
    "param,value",
    [
        ("date_from", "not-a-date"),
        ("date_to", "2026-13-01"),
        ("date_from", "2026-02-30"),
        ("date_to", "%00"),
        ("date_from", "0000-01-01"),
        ("date_from", "20260104"),
        ("date_to", "2026-W01-1"),
        ("date_from", "2026-1-4"),
    ],
)
def test_malformed_dates_are_a_400_naming_the_param(client, setup, param, value):
    _tenant, project = setup
    response = client.get(f"/api/v1/orders/?project={project.id}&{param}={value}")
    assert response.status_code == 400, response.content
    assert param in response.content.decode()


def test_blank_dates_mean_no_filter(client, dated):
    _tenant, project, orders = dated
    body = _list(client, project, date_from="", date_to="")
    assert body["count"] == len(orders)


def test_date_params_ignored_on_detail_routes(client, dated):
    _tenant, project, orders = dated
    response = client.get(f"/api/v1/orders/{orders[0].id}/?date_from=2026-12-01")
    assert response.status_code == 200, response.content
    assert response.json()["number"] == 1


def test_lead_date_range_never_reaches_another_project(client, lead_setup):
    """A date range with no `?project=` must still be RBAC-scoped."""
    tenant, _admin, lead, a, b, order_a, order_b = lead_setup
    _login(client, tenant, lead)
    for url in (
        "/api/v1/orders/?date_from=2000-01-01&date_to=2100-01-01",
        f"/api/v1/orders/?project={b.id}&date_from=2000-01-01",
    ):
        response = client.get(url)
        assert response.status_code == 200, response.content
        assert order_b.id not in {row["id"] for row in response.json()["results"]}, url
    own = client.get("/api/v1/orders/?date_from=2000-01-01")
    assert [row["id"] for row in own.json()["results"]] == [order_a.id]


def test_date_filtered_query_count_does_not_scale_with_results(client, setup):
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    tenant, project = setup
    _seed_matching(tenant, project, 3)  # paid 2026-04-11 onwards (day 100+)

    def count():
        with CaptureQueriesContext(connection) as c:
            response = client.get(
                f"/api/v1/orders/?project={project.id}&date_from=2026-04-01&date_to=2026-12-31"
            )
        assert response.status_code == 200
        return len(c.captured_queries), response.json()["count"]

    count()  # warm
    few, few_n = count()
    _seed_matching(tenant, project, 7)
    many, many_n = count()
    assert (few_n, many_n) == (3, 10)
    assert many == few, f"query count scales with results: {few} -> {many}"


def test_malformed_dates_are_ignored_on_detail_routes(client, dated):
    _tenant, project, orders = dated
    url = f"/api/v1/orders/{orders[0].id}/?date_from=junk&date_to=also-junk"
    assert client.get(url).status_code == 200
    response = client.patch(url, {"notes": "x"}, content_type="application/json")
    assert response.status_code == 200, response.content
