import { describe, expect, it, vi, beforeEach } from "vitest";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { MantineProvider } from "@mantine/core";
import { render } from "@testing-library/react";
import { theme } from "../../theme";
import { ProjectHubScreen } from "./ProjectHubScreen";
import { api } from "../../api/client";
import type { Expense, Me, Paginated, ProjectDetail } from "../../api/types";

vi.mock("../../api/client", async () => {
  const actual = await vi.importActual<typeof import("../../api/client")>("../../api/client");
  return {
    ...actual,
    api: {
      getProjectDetail: vi.fn(),
      updateProjectDetail: vi.fn(),
      listUsers: vi.fn(),
      listProjectAssets: vi.fn(),
      listProjectExpenses: vi.fn(),
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

function makeProjectDetail(overrides: Partial<ProjectDetail> = {}): ProjectDetail {
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
    spend_by_category: [{ category_id: 1, category: "Equipment", total: "1500.00" }],
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
    if (typeof fn === "function" && "mockReset" in fn) (fn as ReturnType<typeof vi.fn>).mockReset();
  });
  mockedApi.exportProjectCsvUrl.mockReturnValue("/api/v1/projects/1/export.csv/");
  mockedApi.exportAssetsCsvUrl.mockReturnValue("/api/v1/exports/assets.csv");
  mockedApi.listUsers.mockResolvedValue(paginated([]));
  mockedApi.listProjectAssets.mockResolvedValue(paginated([]));
  mockedApi.listAllExpenseCategories.mockResolvedValue([]);
});

describe("ProjectHubScreen", () => {
  it("renders all five tabs and the Overview tab's budget figures once loaded", async () => {
    mockMe = makeMe({ project_permissions: { "1": ["project.view", "expense.view"] } });
    mockedApi.getProjectDetail.mockResolvedValue(makeProjectDetail());

    renderHub();

    const tabs = await screen.findByTestId("project-hub-tabs");
    expect(within(tabs).getByTestId("project-tab-overview")).toBeInTheDocument();
    expect(within(tabs).getByTestId("project-tab-assets")).toBeInTheDocument();
    expect(within(tabs).getByTestId("project-tab-expenses")).toBeInTheDocument();
    expect(within(tabs).getByTestId("project-tab-documents")).toBeInTheDocument();
    expect(within(tabs).getByTestId("project-tab-report")).toBeInTheDocument();

    await waitFor(() => expect(screen.getByTestId("overview-budget-tab")).toBeInTheDocument());
    expect(screen.getByText("USD 10000.00")).toBeInTheDocument();
    expect(screen.getAllByText("USD 1500.00").length).toBeGreaterThan(0);
    expect(screen.getByText("USD 8500.00")).toBeInTheDocument();
  });

  it("renders a locked financials affordance (never $0) when budget fields come back null", async () => {
    mockMe = makeMe({ project_permissions: { "1": ["project.view"] } });
    mockedApi.getProjectDetail.mockResolvedValue(
      makeProjectDetail({ budget_total: null, spent: null, remaining: null, spend_by_category: null }),
    );

    renderHub();

    await waitFor(() => expect(screen.getByTestId("financials-locked")).toBeInTheDocument());
    expect(screen.queryByText(/^\$0/)).not.toBeInTheDocument();
    expect(screen.queryByText("USD 0.00")).not.toBeInTheDocument();
  });

  it("shows financials (not the lock panel) for an authorized caller viewing a project with no budget set yet", async () => {
    // `budget_total` is `null` here too, but `spent`/`remaining`/
    // `spend_by_category` are NOT — every project starts with no budget
    // configured, and the backend still returns a real `spent` (at least
    // "0.00") for an authorized caller regardless. `spent` (not
    // `budget_total`) is the only unambiguous redaction sentinel.
    mockMe = makeMe({ project_permissions: { "1": ["project.view", "expense.view"] } });
    mockedApi.getProjectDetail.mockResolvedValue(
      makeProjectDetail({
        budget_total: null,
        spent: "0.00",
        remaining: "0.00",
        spend_by_category: [],
      }),
    );

    renderHub();

    await waitFor(() => expect(screen.getByTestId("overview-budget-tab")).toBeInTheDocument());
    expect(screen.queryByTestId("financials-locked")).not.toBeInTheDocument();
    expect(screen.getByText("Not set")).toBeInTheDocument();
    expect(screen.getAllByText("USD 0.00").length).toBeGreaterThan(0);
    expect(screen.getByText("No expenses recorded yet.")).toBeInTheDocument();
  });

  it("hides the grant-details Save button for a caller without project-scoped project.manage", async () => {
    mockMe = makeMe({ project_permissions: { "1": ["project.view", "expense.view"] } });
    mockedApi.getProjectDetail.mockResolvedValue(makeProjectDetail());

    renderHub();

    await waitFor(() => expect(screen.getByTestId("overview-budget-tab")).toBeInTheDocument());
    expect(screen.queryByTestId("overview-save-button")).not.toBeInTheDocument();
    expect(screen.getByText("Read-only")).toBeInTheDocument();
  });

  it("shows the grant-details Save button and lets a scoped Lead submit an expense", async () => {
    mockMe = makeMe({
      project_permissions: { "1": ["project.view", "project.manage", "expense.view", "expense.manage"] },
    });
    mockedApi.getProjectDetail.mockResolvedValue(makeProjectDetail());
    mockedApi.listProjectExpenses.mockResolvedValue(paginated<Expense>([]));
    mockedApi.listAllExpenseCategories.mockResolvedValue([
      { id: 7, name: "Equipment", is_active: true },
      { id: 8, name: "Travel", is_active: true },
    ]);
    mockedApi.createExpense.mockResolvedValue({
      id: 5,
      project: 1,
      category: 7,
      amount: "42.00",
      currency: "USD",
      date: "2026-02-01",
      vendor: "Acme",
      invoice_number: "",
      description: "",
      asset: null,
      created_by: 1,
      attachments: [],
      created_at: "2026-02-01T00:00:00Z",
      updated_at: "2026-02-01T00:00:00Z",
    });

    renderHub();
    await waitFor(() => expect(screen.getByTestId("overview-budget-tab")).toBeInTheDocument());
    expect(screen.getByTestId("overview-save-button")).toBeInTheDocument();

    const user = userEvent.setup();
    await user.click(screen.getByTestId("project-tab-expenses"));
    await waitFor(() => expect(screen.getByTestId("expenses-tab")).toBeInTheDocument());
    expect(await screen.findByTestId("new-expense-button")).toBeInTheDocument();

    await user.click(screen.getByTestId("new-expense-button"));
    const amountInput = await screen.findByLabelText(/^Amount/);
    await user.type(amountInput, "42");
    await user.type(screen.getByLabelText("Vendor"), "Acme");

    // Category is a real `<Select>` fed from `GET /api/v1/expense-categories`
    // (backend follow-up endpoint) — shows names, submits the id.
    const categorySelect = screen.getByTestId("expense-category-select");
    await user.click(categorySelect);
    expect(await screen.findByText("Equipment")).toBeInTheDocument();
    expect(screen.getByText("Travel")).toBeInTheDocument();
    await user.click(screen.getByText("Equipment"));

    await user.click(screen.getByTestId("expense-form-submit"));

    await waitFor(() => expect(mockedApi.createExpense).toHaveBeenCalledTimes(1));
    const [calledProjectId, payload] = mockedApi.createExpense.mock.calls[0];
    expect(calledProjectId).toBe(1);
    expect(payload.amount).toBe("42");
    expect(payload.vendor).toBe("Acme");
    expect(payload.category).toBe(7);
  });

  it("shows a loading state for the category select while the expense-categories fetch is in flight, then renders resolved names in the ledger", async () => {
    mockMe = makeMe({
      project_permissions: { "1": ["project.view", "expense.view", "expense.manage"] },
    });
    mockedApi.getProjectDetail.mockResolvedValue(makeProjectDetail());
    mockedApi.listProjectExpenses.mockResolvedValue(
      paginated<Expense>([
        {
          id: 9,
          project: 1,
          category: 7,
          amount: "10.00",
          currency: "USD",
          date: "2026-02-01",
          vendor: "Vendor A",
          invoice_number: "",
          description: "",
          asset: null,
          created_by: 1,
          attachments: [],
          created_at: "2026-02-01T00:00:00Z",
          updated_at: "2026-02-01T00:00:00Z",
        },
      ]),
    );
    let resolveCategories: (rows: { id: number; name: string; is_active: boolean }[]) => void = () => {};
    mockedApi.listAllExpenseCategories.mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveCategories = resolve;
        }),
    );

    renderHub();
    const user = userEvent.setup();
    await user.click(await screen.findByTestId("project-tab-expenses"));
    await waitFor(() => expect(screen.getByTestId("expense-row-9")).toBeInTheDocument());

    // Before the categories fetch resolves, the ledger falls back to the
    // bare id — never blocks rendering the rest of the row.
    expect(screen.getByText("Category #7")).toBeInTheDocument();

    await user.click(screen.getByTestId("new-expense-button"));
    await screen.findByText("New expense");
    expect(await screen.findByPlaceholderText("Loading categories…")).toBeInTheDocument();

    resolveCategories([{ id: 7, name: "Equipment", is_active: true }]);

    // The category loads and the ledger row (behind the modal) re-resolves
    // its bare id to the real name — the placeholder-based loading state
    // clears once the fetch settles.
    await waitFor(() =>
      expect(screen.queryByPlaceholderText("Loading categories…")).not.toBeInTheDocument(),
    );
    expect(screen.getByPlaceholderText("(uncategorized)")).toBeInTheDocument();
    expect(screen.getAllByText("Equipment").length).toBeGreaterThan(0);
    expect(screen.queryByText("Category #7")).not.toBeInTheDocument();
  });
});
