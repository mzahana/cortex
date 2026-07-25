import { describe, expect, it } from "vitest";
import { screen } from "@testing-library/react";
import { renderWithProviders as render } from "../../test/render";
import { HistoryRow } from "./HistoryRow";
import type { Asset, Checkout } from "../../api/types";

function makeCheckout(overrides: Partial<Checkout> = {}): Checkout {
  return {
    id: 5,
    asset: 10,
    user: 1,
    reservation: null,
    checked_out_at: "2026-07-01T10:00:00Z",
    due_at: "2026-07-08T10:00:00Z",
    checked_in_at: "2026-07-06T15:30:00Z",
    checkout_condition: "",
    checkin_condition: "",
    is_open: false,
    is_overdue: false,
    created_at: "2026-07-01T10:00:00Z",
    updated_at: "2026-07-06T15:30:00Z",
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
    ...overrides,
  } as Asset;
}

describe("HistoryRow", () => {
  it("renders checked-out and returned dates, asset name, S/N, and no check-in action", () => {
    render(<HistoryRow checkout={makeCheckout()} asset={makeAsset()} />);

    expect(screen.getByTestId("history-row-5")).toBeInTheDocument();
    expect(screen.getByText("Drone Alpha")).toBeInTheDocument();
    expect(screen.getByText(/Checked out/)).toBeInTheDocument();
    expect(screen.getByText(/Returned/)).toBeInTheDocument();
    expect(screen.getByText(/S\/N SN-10/)).toBeInTheDocument();
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
    expect(screen.queryByText(/Check in/i)).not.toBeInTheDocument();
  });

  it("renders the check-in condition when present", () => {
    render(
      <HistoryRow
        checkout={makeCheckout({ checkin_condition: "Minor scuff on housing" })}
        asset={makeAsset()}
      />,
    );

    expect(screen.getByText(/Condition on return: Minor scuff on housing/)).toBeInTheDocument();
  });

  it("omits the condition line when checkin_condition is empty", () => {
    render(<HistoryRow checkout={makeCheckout({ checkin_condition: "" })} asset={makeAsset()} />);

    expect(screen.queryByText(/Condition on return/)).not.toBeInTheDocument();
  });

  it("falls back to 'Asset #<id>' and omits S/N when the asset hasn't resolved yet", () => {
    render(<HistoryRow checkout={makeCheckout()} asset={undefined} />);

    expect(screen.getByText("Asset #10")).toBeInTheDocument();
    expect(screen.queryByText(/S\/N/)).not.toBeInTheDocument();
  });

  it("renders an em dash when checked_in_at is somehow null (defensive)", () => {
    render(<HistoryRow checkout={makeCheckout({ checked_in_at: null })} asset={makeAsset()} />);

    expect(screen.getByText(/Returned —/)).toBeInTheDocument();
  });
});
