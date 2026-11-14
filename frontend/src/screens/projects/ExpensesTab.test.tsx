import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MantineProvider } from "@mantine/core";
import { theme } from "../../theme";
import { ExpensesTab } from "./ExpensesTab";
import { api } from "../../api/client";
import type { Me, Order, Paginated } from "../../api/types";

vi.mock("../../api/client", async () => {
  const actual =
    await vi.importActual<typeof import("../../api/client")>(
      "../../api/client",
    );
  return {
    ...actual,
    api: {
      listOrders: vi.fn(),
      listAllExpenseCategories: vi.fn(),
      listAssets: vi.fn(),
      listPurchases: vi.fn(),
      listProjectAssets: vi.fn(),
    },
  };
});

const mockMe = {
  id: 1,
  email: "u@example.test",
  name: "U",
  tenant: { id: 1, name: "T", slug: "t" },
  memberships: [],
  permissions: [],
  project_permissions: { "1": ["project.view", "expense.view"] },
} as unknown as Me;

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

function makeOrder(id: number, vendor: string): Order {
  return {
    id,
    number: id,
    project: 1,
    vendor,
    paid_on: "2026-02-01",
    amount: "10.00",
    currency: "SAR",
    method: "card",
    account_label: "",
    statement_ref: "",
    notes: "",
    splits: [],
    attachments: [],
    items_total_settled: "10.00",
    variance: "0.00",
    is_balanced: true,
    created_by: 1,
    created_at: "2026-02-01T00:00:00Z",
    updated_at: "2026-02-01T00:00:00Z",
  } as unknown as Order;
}

function page(
  results: Order[],
  count: number,
  next: string | null,
): Paginated<Order> {
  return { count, next, previous: null, results };
}

function renderTab() {
  return render(
    <MantineProvider theme={theme} defaultColorScheme="light">
      <ExpensesTab projectId={1} />
    </MantineProvider>,
  );
}

beforeEach(() => {
  Object.values(mockedApi).forEach((fn) => fn.mockReset());
  mockedApi.listAllExpenseCategories.mockResolvedValue([]);
  mockedApi.listAssets.mockResolvedValue(page([], 0, null) as never);
  mockedApi.listPurchases.mockResolvedValue(page([], 0, null) as never);
  mockedApi.listProjectAssets.mockResolvedValue(page([], 0, null) as never);
});

describe("ExpensesTab search and paging", () => {
  it("initial load asks for page 1 with no search term", async () => {
    mockedApi.listOrders.mockResolvedValue(page([makeOrder(1, "Alpha")], 1, null));
    renderTab();
    await screen.findByText(/Alpha/);
    expect(mockedApi.listOrders).toHaveBeenCalledWith(1, {
      search: undefined,
      page: 1,
    });
    expect(screen.getByTestId("expense-count")).toHaveTextContent("1 expense");
    expect(screen.queryByTestId("expenses-load-more")).not.toBeInTheDocument();
  });

  it("typing in the search box passes the trimmed term to listOrders", async () => {
    mockedApi.listOrders.mockResolvedValue(page([makeOrder(1, "Alpha")], 1, null));
    renderTab();
    await screen.findByText(/Alpha/);

    mockedApi.listOrders.mockResolvedValue(page([makeOrder(7, "Bravo")], 1, null));
    const user = userEvent.setup();
    await user.type(screen.getByTestId("expense-search"), "  bravo ");

    await waitFor(() =>
      expect(mockedApi.listOrders).toHaveBeenLastCalledWith(1, {
        search: "bravo",
        page: 1,
      }),
    );
    await screen.findByText(/Bravo/);
    expect(screen.queryByText(/Alpha/)).not.toBeInTheDocument();
    expect(screen.getByTestId("expense-count")).toHaveTextContent(
      "1 matching expense",
    );
  });

  it("shows an empty-search message when nothing matches", async () => {
    mockedApi.listOrders.mockResolvedValueOnce(page([makeOrder(1, "Alpha")], 1, null));
    renderTab();
    await screen.findByText(/Alpha/);
    mockedApi.listOrders.mockResolvedValue(page([], 0, null));
    await userEvent.setup().type(screen.getByTestId("expense-search"), "zzz");
    expect(
      await screen.findByText("No expenses match these filters."),
    ).toBeInTheDocument();
  });

  it("Show more appends page 2 and hides itself on the last page", async () => {
    mockedApi.listOrders.mockResolvedValueOnce(
      page([makeOrder(1, "Alpha"), makeOrder(2, "Bravo")], 3, "http://x/?page=2"),
    );
    renderTab();
    await screen.findByText(/Alpha/);
    expect(screen.getByTestId("expense-count")).toHaveTextContent("showing 2");
    expect(screen.getByTestId("expenses-load-more")).toHaveTextContent(
      "1 remaining",
    );

    mockedApi.listOrders.mockResolvedValueOnce(
      page([makeOrder(3, "Charlie")], 3, null),
    );
    await userEvent.setup().click(screen.getByTestId("expenses-load-more"));

    await screen.findByText(/Charlie/);
    expect(mockedApi.listOrders).toHaveBeenLastCalledWith(1, {
      search: undefined,
      page: 2,
    });
    // Page 1 rows are kept, not replaced.
    expect(screen.getByText(/Alpha/)).toBeInTheDocument();
    expect(screen.getByText(/Bravo/)).toBeInTheDocument();
    await waitFor(() =>
      expect(screen.queryByTestId("expenses-load-more")).not.toBeInTheDocument(),
    );
  });

  it("Show more drops a boundary row that page 2 repeats", async () => {
    mockedApi.listOrders.mockResolvedValueOnce(
      page([makeOrder(1, "Alpha"), makeOrder(2, "Bravo")], 3, "http://x/?page=2"),
    );
    renderTab();
    await screen.findByText(/Alpha/);
    // An order added meanwhile shifts rows: Bravo comes back on page 2.
    mockedApi.listOrders.mockResolvedValueOnce(
      page([makeOrder(2, "Bravo"), makeOrder(3, "Charlie")], 3, null),
    );
    await userEvent.setup().click(screen.getByTestId("expenses-load-more"));
    await screen.findByText(/Charlie/);
    expect(screen.getAllByText(/Bravo/)).toHaveLength(1);
  });

  it("a failed Show more shows an error, and a retry clears it", async () => {
    mockedApi.listOrders.mockResolvedValueOnce(
      page([makeOrder(1, "Alpha")], 2, "http://x/?page=2"),
    );
    renderTab();
    await screen.findByText(/Alpha/);
    mockedApi.listOrders.mockRejectedValueOnce(new Error("boom"));
    const user = userEvent.setup();
    await user.click(screen.getByTestId("expenses-load-more"));
    await screen.findByText("Could not load more expenses.");
    mockedApi.listOrders.mockResolvedValueOnce(
      page([makeOrder(2, "Bravo")], 2, null),
    );
    await user.click(screen.getByTestId("expenses-load-more"));
    await screen.findByText(/Bravo/);
    expect(
      screen.queryByText("Could not load more expenses."),
    ).not.toBeInTheDocument();
  });
});

describe("ExpensesTab payment-date range", () => {
  it("typing a From date sends date_from and keeps To open", async () => {
    mockedApi.listOrders.mockResolvedValue(page([makeOrder(1, "Alpha")], 1, null));
    renderTab();
    await screen.findByText(/Alpha/);

    const user = userEvent.setup();
    await user.type(screen.getByTestId("expense-date-from"), "2026-01-04");
    await user.tab();

    await waitFor(() =>
      expect(mockedApi.listOrders).toHaveBeenLastCalledWith(1, {
        search: undefined,
        date_from: "2026-01-04",
        date_to: undefined,
        page: 1,
      }),
    );
  });

  it("a full range combines with the search term", async () => {
    mockedApi.listOrders.mockResolvedValue(page([makeOrder(1, "Alpha")], 1, null));
    renderTab();
    await screen.findByText(/Alpha/);

    const user = userEvent.setup();
    await user.type(screen.getByTestId("expense-date-from"), "2026-01-01");
    await user.tab();
    await user.type(screen.getByTestId("expense-date-to"), "2026-03-31");
    await user.tab();
    await user.type(screen.getByTestId("expense-search"), "amazon");

    await waitFor(() =>
      expect(mockedApi.listOrders).toHaveBeenLastCalledWith(1, {
        search: "amazon",
        date_from: "2026-01-01",
        date_to: "2026-03-31",
        page: 1,
      }),
    );
  });

  it("an empty filtered result says no expenses match the filters", async () => {
    mockedApi.listOrders.mockResolvedValue(page([makeOrder(1, "Alpha")], 1, null));
    renderTab();
    await screen.findByText(/Alpha/);

    mockedApi.listOrders.mockResolvedValue(page([], 0, null));
    const user = userEvent.setup();
    await user.type(screen.getByTestId("expense-date-from"), "2030-01-01");
    await user.tab();

    await screen.findByText("No expenses match these filters.");
    expect(screen.getByTestId("expense-count")).toHaveTextContent("0 matching expenses");
  });

  it("Show more keeps the date range on the next page", async () => {
    mockedApi.listOrders.mockResolvedValue(page([makeOrder(1, "Alpha")], 1, null));
    renderTab();
    await screen.findByText(/Alpha/);

    // Answer by params: typing a date fires intermediate loads, so a
    // one-shot mock would be consumed by the wrong call.
    mockedApi.listOrders.mockImplementation(async (_id, params) => {
      if (params?.date_to !== "2026-06-30") return page([makeOrder(1, "Alpha")], 1, null);
      return params.page === 2
        ? page([makeOrder(3, "Charlie")], 2, null)
        : page([makeOrder(2, "Bravo")], 2, "next-url");
    });
    const user = userEvent.setup();
    await user.type(screen.getByTestId("expense-date-to"), "2026-06-30");
    await user.tab();
    await screen.findByText(/Bravo/);

    await user.click(screen.getByTestId("expenses-load-more"));
    await screen.findByText(/Charlie/);
    expect(mockedApi.listOrders).toHaveBeenLastCalledWith(1, {
      search: undefined,
      date_from: undefined,
      date_to: "2026-06-30",
      page: 2,
    });
  });

  it("half-typed dates never fire a request; only the complete date does", async () => {
    mockedApi.listOrders.mockResolvedValue(page([makeOrder(1, "Alpha")], 1, null));
    renderTab();
    await screen.findByText(/Alpha/);
    const callsBefore = mockedApi.listOrders.mock.calls.length;

    const user = userEvent.setup();
    await user.type(screen.getByTestId("expense-date-from"), "2026-01-04");
    await waitFor(() =>
      expect(mockedApi.listOrders).toHaveBeenLastCalledWith(1, {
        search: undefined,
        date_from: "2026-01-04",
        date_to: undefined,
        page: 1,
      }),
    );
    const dateCalls = mockedApi.listOrders.mock.calls.slice(callsBefore);
    expect(dateCalls).toHaveLength(1);
    expect(screen.queryByText("Could not load expenses.")).not.toBeInTheDocument();
  });

  it("an impossible date (Feb 30) is never sent", async () => {
    mockedApi.listOrders.mockResolvedValue(page([makeOrder(1, "Alpha")], 1, null));
    renderTab();
    await screen.findByText(/Alpha/);
    const callsBefore = mockedApi.listOrders.mock.calls.length;

    const user = userEvent.setup();
    await user.type(screen.getByTestId("expense-date-from"), "2026-02-30");
    await user.tab();
    expect(mockedApi.listOrders.mock.calls.length).toBe(callsBefore);
  });

  it("clearing the date removes the filter", async () => {
    mockedApi.listOrders.mockResolvedValue(page([makeOrder(1, "Alpha")], 1, null));
    renderTab();
    await screen.findByText(/Alpha/);

    const user = userEvent.setup();
    const input = screen.getByTestId("expense-date-to");
    await user.type(input, "2026-06-30");
    await waitFor(() =>
      expect(mockedApi.listOrders).toHaveBeenLastCalledWith(
        1,
        expect.objectContaining({ date_to: "2026-06-30" }),
      ),
    );
    await user.clear(input);
    await waitFor(() =>
      expect(mockedApi.listOrders).toHaveBeenLastCalledWith(1, {
        search: undefined,
        date_from: undefined,
        date_to: undefined,
        page: 1,
      }),
    );
  });
});

