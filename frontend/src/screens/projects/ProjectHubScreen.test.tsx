import { describe, expect, it, vi, beforeEach } from "vitest";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { MantineProvider } from "@mantine/core";
import { render } from "@testing-library/react";
import { theme } from "../../theme";
import { ProjectHubScreen } from "./ProjectHubScreen";
import { api } from "../../api/client";
import type { Order, Me, Paginated, ProjectDetail } from "../../api/types";

vi.mock("../../api/client", async () => {
  const actual =
    await vi.importActual<typeof import("../../api/client")>(
      "../../api/client",
    );
  return {
    ...actual,
    api: {
      getProjectDetail: vi.fn(),
      updateProjectDetail: vi.fn(),
      listUsers: vi.fn(),
      listProjectAssets: vi.fn(),
      listAssets: vi.fn(),
      listPurchases: vi.fn(),
      listProjectExpenses: vi.fn(),
      listOrders: vi.fn(),
      generateExpensePack: vi.fn(),
      generateAuditChecklist: vi.fn(),
      createOrder: vi.fn(),
      updateOrder: vi.fn(),
      deleteOrder: vi.fn(),
      listAllExpenseCategories: vi.fn(),
      createExpense: vi.fn(),
      updateExpense: vi.fn(),
      deleteExpense: vi.fn(),
      uploadExpenseAttachment: vi.fn(),
      listProjectDocuments: vi.fn(),
      uploadProjectDocument: vi.fn(),
      deleteProjectDocument: vi.fn(),
      generateProjectReport: vi.fn(),
      getJob: vi.fn(),
      exportProjectCsvUrl: vi.fn(() => "/api/v1/projects/1/export.csv/"),
      listAllCategories: vi.fn(),
      listAllLocations: vi.fn(),
      listAllTags: vi.fn(),
      listAllProjects: vi.fn(),
      exportAssetsCsvUrl: vi.fn(() => "/api/v1/exports/assets.csv"),
    },
  };
});

let mockMe: Me | null = null;
vi.mock("../../hooks/useAuth", () => ({
  useAuth: () => ({
    status: "authenticated",
    me: mockMe,
    error: null,
    login: vi.fn(),
    logout: vi.fn(),
    refresh: vi.fn(),
    logoutAll: vi.fn(),
  }),
}));

const mockedApi = vi.mocked(api);

function makeMe(overrides: Partial<Me> = {}): Me {
  return {
    id: 1,
    email: "user@example.test",
    name: "Test User",
    tenant: { id: 1, name: "T", slug: "t" },
    memberships: [],
    permissions: [],
    project_permissions: {},
    ...overrides,
  } as Me;
}

function makeProjectDetail(
  overrides: Partial<ProjectDetail> = {},
): ProjectDetail {
  return {
    id: 1,
    name: "Robotics Grant",
    code: "NSF-1",
    lead_user: null,
    is_active: true,
    funding_source: "external",
    sponsor: "NSF",
    start_date: "2026-01-01",
    end_date: null,
    budget_total: "10000.00",
    currency: "USD",
    status: "active",
    description: "",
    created_at: "2026-01-01T00:00:00Z",
    spent: "1500.00",
    remaining: "8500.00",
    spend_by_category: [
      { category_id: 1, category: "Equipment", total: "1500.00" },
    ],
    ...overrides,
  };
}

function paginated<T>(results: T[]): Paginated<T> {
  return { count: results.length, next: null, previous: null, results };
}

function renderHub() {
  return render(
    <MantineProvider theme={theme} defaultColorScheme="light">
      <MemoryRouter initialEntries={["/projects/1"]}>
        <Routes>
          <Route path="/projects/:id" element={<ProjectHubScreen />} />
        </Routes>
      </MemoryRouter>
    </MantineProvider>,
  );
}

beforeEach(() => {
  mockMe = null;
  Object.values(mockedApi).forEach((fn) => {
    if (typeof fn === "function" && "mockReset" in fn)
      (fn as ReturnType<typeof vi.fn>).mockReset();
  });
  mockedApi.exportProjectCsvUrl.mockReturnValue(
    "/api/v1/projects/1/export.csv/",
  );
  mockedApi.exportAssetsCsvUrl.mockReturnValue("/api/v1/exports/assets.csv");
  mockedApi.listUsers.mockResolvedValue(paginated([]));
  mockedApi.listProjectAssets.mockResolvedValue(paginated([]));
  // M8: the expense form also offers unassigned (general-pool) assets.
  mockedApi.listAssets.mockResolvedValue(paginated([]));
  // M8 Phase 2: the expense form offers a receipt to attach the item to.
  mockedApi.listPurchases.mockResolvedValue(paginated([]));
  // M8 Phase 2 (rev): the Expenses tab lists ORDERS.
  mockedApi.listOrders.mockResolvedValue(paginated([]));
  mockedApi.listAllExpenseCategories.mockResolvedValue([]);
});

/** A minimal `Order` for the Expenses tab — one shipment, one item, which is
 * what a simple order looks like. */
function makeOrder(overrides: Partial<Order> = {}): Order {
  return {
    id: 5,
    number: 1,
    project: 1,
    vendor: "Amazon",
    paid_on: "2026-02-01",
    amount: "359.00",
    currency: "SAR",
    method: "card",
    account_label: "",
    statement_ref: "",
    notes: "",
    splits: [
      {
        id: 1,
        receipt_number: "",
        currency: "SAR",
        shipping: "0.00",
        tax: "0.00",
        total: "359.00",
        settled_amount: null,
        items: [{ id: 2, description: "GPU", amount: "350.00", asset: null }],
      },
    ],
    attachments: [],
    items_total_settled: "359.00",
    variance: "0.00",
    is_balanced: true,
    created_by: 1,
    created_at: "2026-02-01T00:00:00Z",
    updated_at: "2026-02-01T00:00:00Z",
    ...overrides,
  };
}

describe("ProjectHubScreen", () => {
  it("renders all five tabs and the Overview tab's budget figures once loaded", async () => {
    mockMe = makeMe({
      project_permissions: { "1": ["project.view", "expense.view"] },
    });
    mockedApi.getProjectDetail.mockResolvedValue(makeProjectDetail());

    renderHub();

    const tabs = await screen.findByTestId("project-hub-tabs");
    expect(
      within(tabs).getByTestId("project-tab-overview"),
    ).toBeInTheDocument();
    expect(within(tabs).getByTestId("project-tab-assets")).toBeInTheDocument();
    expect(
      within(tabs).getByTestId("project-tab-expenses"),
    ).toBeInTheDocument();
    expect(
      within(tabs).getByTestId("project-tab-documents"),
    ).toBeInTheDocument();
    expect(within(tabs).getByTestId("project-tab-report")).toBeInTheDocument();

    await waitFor(() =>
      expect(screen.getByTestId("overview-budget-tab")).toBeInTheDocument(),
    );
    expect(screen.getByText("USD 10000.00")).toBeInTheDocument();
    expect(screen.getAllByText("USD 1500.00").length).toBeGreaterThan(0);
    expect(screen.getByText("USD 8500.00")).toBeInTheDocument();
  });

  it("renders a locked financials affordance (never $0) when budget fields come back null", async () => {
    mockMe = makeMe({ project_permissions: { "1": ["project.view"] } });
    mockedApi.getProjectDetail.mockResolvedValue(
      makeProjectDetail({
        budget_total: null,
        spent: null,
        remaining: null,
        spend_by_category: null,
      }),
    );

    renderHub();

    await waitFor(() =>
      expect(screen.getByTestId("financials-locked")).toBeInTheDocument(),
    );
    expect(screen.queryByText(/^\$0/)).not.toBeInTheDocument();
    expect(screen.queryByText("USD 0.00")).not.toBeInTheDocument();
  });

  it("shows financials (not the lock panel) for an authorized caller viewing a project with no budget set yet", async () => {
    // `budget_total` is `null` here too, but `spent`/`remaining`/
    // `spend_by_category` are NOT — every project starts with no budget
    // configured, and the backend still returns a real `spent` (at least
    // "0.00") for an authorized caller regardless. `spent` (not
    // `budget_total`) is the only unambiguous redaction sentinel.
    mockMe = makeMe({
      project_permissions: { "1": ["project.view", "expense.view"] },
    });
    mockedApi.getProjectDetail.mockResolvedValue(
      makeProjectDetail({
        budget_total: null,
        spent: "0.00",
        remaining: "0.00",
        spend_by_category: [],
      }),
    );

    renderHub();

    await waitFor(() =>
      expect(screen.getByTestId("overview-budget-tab")).toBeInTheDocument(),
    );
    expect(screen.queryByTestId("financials-locked")).not.toBeInTheDocument();
    expect(screen.getByText("Not set")).toBeInTheDocument();
    expect(screen.getAllByText("USD 0.00").length).toBeGreaterThan(0);
    expect(screen.getByText("No expenses recorded yet.")).toBeInTheDocument();
  });

  it("hides the grant-details Save button for a caller without project-scoped project.manage", async () => {
    mockMe = makeMe({
      project_permissions: { "1": ["project.view", "expense.view"] },
    });
    mockedApi.getProjectDetail.mockResolvedValue(makeProjectDetail());

    renderHub();

    await waitFor(() =>
      expect(screen.getByTestId("overview-budget-tab")).toBeInTheDocument(),
    );
    expect(
      screen.queryByTestId("overview-save-button"),
    ).not.toBeInTheDocument();
    expect(screen.getByText("Read-only")).toBeInTheDocument();
  });

  it("shows the grant-details Save button and lets a scoped Lead record an order", async () => {
    mockMe = makeMe({
      project_permissions: {
        "1": [
          "project.view",
          "project.manage",
          "expense.view",
          "expense.manage",
        ],
      },
    });
    mockedApi.getProjectDetail.mockResolvedValue(makeProjectDetail());
    mockedApi.listOrders.mockResolvedValue(paginated<Order>([]));
    mockedApi.listAllExpenseCategories.mockResolvedValue([
      { id: 7, name: "Equipment", is_active: true },
    ]);
    mockedApi.createOrder.mockResolvedValue(makeOrder());

    renderHub();
    await waitFor(() =>
      expect(screen.getByTestId("overview-budget-tab")).toBeInTheDocument(),
    );
    expect(screen.getByTestId("overview-save-button")).toBeInTheDocument();

    const user = userEvent.setup();
    await user.click(screen.getByTestId("project-tab-expenses"));
    await waitFor(() =>
      expect(screen.getByTestId("expenses-tab")).toBeInTheDocument(),
    );

    // ONE button, ONE form — the whole order is entered in a single place.
    await user.click(await screen.findByTestId("add-expense"));
    await user.type(await screen.findByTestId("order-vendor"), "Amazon");
    await user.type(screen.getByTestId("order-amount"), "359.00");
    await user.type(screen.getByTestId("order-item-desc-0-0"), "GPU");
    await user.type(screen.getByTestId("order-item-amount-0-0"), "350.00");
    await user.click(screen.getByTestId("order-save"));

    await waitFor(() => expect(mockedApi.createOrder).toHaveBeenCalledTimes(1));
    const payload = mockedApi.createOrder.mock.calls[0][0];
    expect(payload.project).toBe(1);
    expect(payload.vendor).toBe("Amazon");
    expect(payload.amount).toBe("359.00");
    // The order carries its items nested — not a second, separate request.
    expect(payload.splits?.[0].items[0].description).toBe("GPU");
    expect(payload.splits?.[0].items[0].amount).toBe("350.00");
  });

  it("a simple order never shows the shipment concept, and a split one does", async () => {
    mockMe = makeMe({
      project_permissions: {
        "1": ["project.view", "expense.view", "expense.manage"],
      },
    });
    mockedApi.getProjectDetail.mockResolvedValue(makeProjectDetail());
    mockedApi.listAllExpenseCategories.mockResolvedValue([]);
    mockedApi.listOrders.mockResolvedValue(
      paginated<Order>([
        makeOrder({
          id: 9,
          vendor: "Vendor A",
          amount: "10.00",
          splits: [
            {
              id: 1,
              receipt_number: "",
              currency: "SAR",
              shipping: "0.00",
              tax: "0.00",
              total: "10.00",
              settled_amount: null,
              items: [
                { id: 3, description: "Cable", amount: "10.00", asset: null },
              ],
            },
          ],
        }),
      ]),
    );

    renderHub();
    const user = userEvent.setup();
    await user.click(await screen.findByTestId("project-tab-expenses"));

    // The order is listed by what the user recognizes: vendor and total.
    await waitFor(() =>
      expect(screen.getByText(/Vendor A/)).toBeInTheDocument(),
    );
    expect(screen.getByText(/1 item/)).toBeInTheDocument();
    // A single-shipment order must not mention shipments at all.
    expect(screen.queryByText(/shipments/)).not.toBeInTheDocument();
  });
});
