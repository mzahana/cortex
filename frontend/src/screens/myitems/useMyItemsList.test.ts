import { describe, expect, it, vi } from "vitest";
import { act, renderHook, waitFor } from "@testing-library/react";
import { useMyItemsList } from "./useMyItemsList";
import { api } from "../../api/client";
import type { Checkout, Paginated } from "../../api/types";

vi.mock("../../api/client", async () => {
  const actual = await vi.importActual<typeof import("../../api/client")>("../../api/client");
  return {
    ...actual,
    api: {
      listCheckouts: vi.fn(),
      getAsset: vi.fn(),
    },
  };
});

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

function paginated(results: Checkout[]): Paginated<Checkout> {
  return { count: results.length, next: null, previous: null, results };
}

describe("useMyItemsList", () => {
  it("fetches page 1 of open checkouts, ordered by due date, on mount", async () => {
    mockedApi.listCheckouts.mockResolvedValue(paginated([makeCheckout()]));

    const { result } = renderHook(() => useMyItemsList(true));

    await waitFor(() => expect(result.current.loading).toBe(false));

    expect(mockedApi.listCheckouts).toHaveBeenCalledWith({
      open: true,
      ordering: "due_at",
      page: 1,
      page_size: 25,
    });
    expect(result.current.items).toHaveLength(1);
  });

  it("fetches history (open: false) ordered by most-recently-returned", async () => {
    mockedApi.listCheckouts.mockResolvedValue(
      paginated([makeCheckout({ id: 2, checked_in_at: "2026-07-10T10:00:00Z", is_open: false })]),
    );

    const { result } = renderHook(() => useMyItemsList(false));

    await waitFor(() => expect(result.current.loading).toBe(false));

    expect(mockedApi.listCheckouts).toHaveBeenCalledWith({
      open: false,
      ordering: "-checked_in_at",
      page: 1,
      page_size: 25,
    });
  });

  it("re-fetches page 1 when `open` toggles, even if the previous instance was on a later page", async () => {
    mockedApi.listCheckouts.mockResolvedValue(paginated([makeCheckout()]));

    const { result, rerender } = renderHook(({ open }) => useMyItemsList(open), {
      initialProps: { open: true },
    });
    await waitFor(() => expect(result.current.loading).toBe(false));

    // Move to page 2 on the "current" instance.
    act(() => result.current.setPage(2));
    await waitFor(() => expect(result.current.page).toBe(2));

    mockedApi.listCheckouts.mockClear();

    // Now toggle to history — should reset to page 1, not continue from page 2.
    rerender({ open: false });
    await waitFor(() => expect(result.current.loading).toBe(false));

    expect(mockedApi.listCheckouts).toHaveBeenCalledWith({
      open: false,
      ordering: "-checked_in_at",
      page: 1,
      page_size: 25,
    });
    expect(result.current.page).toBe(1);
  });

  it("keeps two independent instances (current/history) reloading separately", async () => {
    mockedApi.listCheckouts.mockImplementation((params) =>
      Promise.resolve(
        paginated([makeCheckout({ id: params?.open ? 1 : 2, is_open: !!params?.open })]),
      ),
    );

    const current = renderHook(() => useMyItemsList(true));
    const history = renderHook(() => useMyItemsList(false));

    await waitFor(() => expect(current.result.current.loading).toBe(false));
    await waitFor(() => expect(history.result.current.loading).toBe(false));

    expect(current.result.current.items[0].id).toBe(1);
    expect(history.result.current.items[0].id).toBe(2);

    mockedApi.listCheckouts.mockClear();

    // Reloading `current` alone must not trigger a fetch for `history`.
    act(() => current.result.current.reload());
    await waitFor(() => expect(mockedApi.listCheckouts).toHaveBeenCalledTimes(1));
    expect(mockedApi.listCheckouts).toHaveBeenCalledWith(
      expect.objectContaining({ open: true }),
    );
  });

  it("surfaces a server error without leaving stale items", async () => {
    const { ApiError } = await vi.importActual<typeof import("../../api/client")>(
      "../../api/client",
    );
    mockedApi.listCheckouts.mockRejectedValue(
      new ApiError({ status: 500, title: "Server error", type: "about:blank" }),
    );

    const { result } = renderHook(() => useMyItemsList(true));

    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.error).toBeTruthy();
    expect(result.current.items).toEqual([]);
  });
});
