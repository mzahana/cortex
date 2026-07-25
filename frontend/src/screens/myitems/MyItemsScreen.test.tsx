import { describe, expect, it, vi } from "vitest";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { renderWithProviders } from "../../test/render";
import { MyItemsScreen } from "./MyItemsScreen";
import { api } from "../../api/client";
import type { Asset, Checkout, Paginated } from "../../api/types";

vi.mock("../../api/client", async () => {
  const actual = await vi.importActual<typeof import("../../api/client")>("../../api/client");
  return {
    ...actual,
    api: {
      listCheckouts: vi.fn(),
      getAsset: vi.fn(),
      checkinCheckout: vi.fn(),
    },
  };
});

// `AppLayout` requires an `AuthProvider` ancestor (`useAuth()` throws
// without one) and itself fetches `/me` on mount — irrelevant to this
// screen's own behavior, so stub the hook directly rather than wiring up
// the real provider + a mocked `api.me()`.
vi.mock("../../hooks/useAuth", () => ({
  useAuth: () => ({
    status: "authenticated",
    me: { id: 1, email: "a@b.com", name: "A", tenant: { id: 1, name: "T", slug: "t" }, memberships: [], permissions: [], project_permissions: {} },
    error: null,
    login: vi.fn(),
    logout: vi.fn(),
    refresh: vi.fn(),
  }),
}));

const mockedApi = vi.mocked(api);

function makeCheckout(overrides: Partial<Checkout> = {}): Checkout {
  return {
    id: 1,
    asset: 10,
    user: 1,
    reservation: null,
    checked_out_at: "2026-07-01T10:00:00Z",
    due_at: "2026-07-08T10:00:00Z",
    checked_in_at: null,
    checkout_condition: "",
    checkin_condition: "",
    is_open: true,
    is_overdue: false,
    created_at: "2026-07-01T10:00:00Z",
    updated_at: "2026-07-01T10:00:00Z",
    ...overrides,
  };
}

function makeAsset(overrides: Partial<Asset> = {}): Asset {
  return {
    id: 10,
    uuid: "uuid-10",
    qr_token: "qr-10",
    category: 1,
    name: "Drone Alpha",
    description: "",
    is_consumable: false,
    project: null,
    serial_number: "SN-10",
    manufacturer: "",
    model: "",
    location: null,
    purchase_date: null,
    purchase_cost: null,
    currency: "USD",
    warranty_expiry: null,
    supplier: "",
    status: "available",
    condition: "good",
    retired_at: null,
    // Additional fields the `Asset` interface declares beyond this point
    // (if any) are intentionally omitted; TS will flag it if required.
    ...overrides,
  } as Asset;
}

function paginated<T>(results: T[]): Paginated<T> {
  return { count: results.length, next: null, previous: null, results };
}

function renderScreen() {
  return renderWithProviders(<MyItemsScreen />);
}

describe("MyItemsScreen", () => {
  it("renders the segmented control defaulting to the Current tab and shows open checkouts via CheckoutRow", async () => {
    mockedApi.listCheckouts.mockImplementation((params) =>
      Promise.resolve(
        params?.open ? paginated([makeCheckout()]) : paginated([]),
      ),
    );
    mockedApi.getAsset.mockResolvedValue(makeAsset());

    renderScreen();

    const tabs = await screen.findByTestId("my-items-tab");
    expect(within(tabs).getByText("Current")).toBeInTheDocument();
    expect(within(tabs).getByText("History")).toBeInTheDocument();

    await waitFor(() => expect(screen.getByTestId("checkout-row-1")).toBeInTheDocument());
    expect(screen.getByTestId("checkin-1")).toBeInTheDocument();
    expect(screen.queryByTestId(/^history-row-/)).not.toBeInTheDocument();
  });

  it("switching to the History tab shows returned items via HistoryRow, with no check-in button", async () => {
    mockedApi.listCheckouts.mockImplementation((params) =>
      Promise.resolve(
        params?.open
          ? paginated([makeCheckout()])
          : paginated([
              makeCheckout({
                id: 2,
                is_open: false,
                checked_in_at: "2026-07-10T09:00:00Z",
                checkin_condition: "good",
              }),
            ]),
      ),
    );
    mockedApi.getAsset.mockResolvedValue(makeAsset());

    renderScreen();
    await waitFor(() => expect(screen.getByTestId("checkout-row-1")).toBeInTheDocument());

    const user = userEvent.setup();
    await user.click(screen.getByText("History"));

    await waitFor(() => expect(screen.getByTestId("history-row-2")).toBeInTheDocument());
    expect(screen.queryByTestId("checkin-2")).not.toBeInTheDocument();
    expect(screen.queryByTestId(/^checkout-row-/)).not.toBeInTheDocument();
  });

  it("shows tab-specific empty-state copy", async () => {
    mockedApi.listCheckouts.mockResolvedValue(paginated([]));

    renderScreen();

    await waitFor(() => expect(screen.getByText("Nothing checked out")).toBeInTheDocument());

    const user = userEvent.setup();
    await user.click(screen.getByText("History"));

    await waitFor(() => expect(screen.getByText("No history yet")).toBeInTheDocument());
    expect(screen.queryByText("Nothing checked out")).not.toBeInTheDocument();
  });

  it("checking in an item on Current reloads both tabs", async () => {
    mockedApi.listCheckouts.mockImplementation((params) =>
      Promise.resolve(
        params?.open ? paginated([makeCheckout()]) : paginated([]),
      ),
    );
    mockedApi.getAsset.mockResolvedValue(makeAsset());
    mockedApi.checkinCheckout.mockResolvedValue(
      makeCheckout({ is_open: false, checked_in_at: "2026-07-11T00:00:00Z" }),
    );

    renderScreen();
    await waitFor(() => expect(screen.getByTestId("checkout-row-1")).toBeInTheDocument());

    mockedApi.listCheckouts.mockClear();

    const user = userEvent.setup();
    await user.click(screen.getByTestId("checkin-1"));

    await waitFor(() => expect(mockedApi.checkinCheckout).toHaveBeenCalledWith(1));
    // Both the "current" and "history" hook instances refetch after check-in.
    await waitFor(() => {
      const calls = mockedApi.listCheckouts.mock.calls.map((c) => c[0]?.open);
      expect(calls).toEqual(expect.arrayContaining([true, false]));
    });
  });
});
