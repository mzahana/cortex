import { beforeEach, describe, expect, it, vi } from "vitest";
import { screen, waitFor, render } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MantineProvider } from "@mantine/core";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { theme } from "../../theme";
import { api, ApiError } from "../../api/client";
import { AssetFormScreen } from "./AssetForm";
import { makeAsset, ME } from "./countedAssets.testutil";

vi.mock("../../api/client", async () => {
  const actual = await vi.importActual<typeof import("../../api/client")>("../../api/client");
  return {
    ...actual,
    api: {
      listAllCategories: vi.fn(),
      listAllLocations: vi.fn(),
      listAllProjects: vi.fn(),
      listCategoryFields: vi.fn(),
      getAsset: vi.fn(),
      createAsset: vi.fn(),
      updateAsset: vi.fn(),
    },
  };
});
vi.mock("../../hooks/useAuth", () => ({ useAuth: () => ({ status: "authenticated", me: ME }) }));
vi.mock("../../layout/AppLayout", () => ({
  AppLayout: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
}));

const m = vi.mocked(api);
const category = {
  id: 1,
  name: "Tools",
  parent: null,
  default_is_consumable: false,
  requires_approval: false,
  requires_calibration: false,
  field_defs: [],
  created_at: "",
};

function renderForm(path: string) {
  return render(
    <MantineProvider theme={theme}>
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route path="/assets/new" element={<AssetFormScreen />} />
          <Route path="/assets/:id/edit" element={<AssetFormScreen />} />
          <Route path="/assets/:id" element={<div>detail</div>} />
        </Routes>
      </MemoryRouter>
    </MantineProvider>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  m.listAllCategories.mockResolvedValue([category] as never);
  m.listAllLocations.mockResolvedValue([]);
  m.listAllProjects.mockResolvedValue([]);
  m.listCategoryFields.mockResolvedValue([]);
  m.createAsset.mockResolvedValue(makeAsset({ id: 99 }));
  m.updateAsset.mockResolvedValue(makeAsset());
});

describe("AssetForm tracking", () => {
  it("create: quantity only appears for Counted; payload carries tracking+quantity", async () => {
    renderForm("/assets/new");
    await userEvent.type(await screen.findByTestId("asset-form-name"), "Screwdriver");
    expect(screen.queryByTestId("asset-form-quantity")).toBeNull();
    expect(screen.getByText(/can be reserved and checked out/i)).toBeInTheDocument();

    await userEvent.click(screen.getByText("Counted"));
    const qty = await screen.findByTestId("asset-form-quantity");
    expect(screen.getByText(/not reservable/i)).toBeInTheDocument();
    await userEvent.clear(qty);
    await userEvent.type(qty, "10");

    await userEvent.click(screen.getByRole("textbox", { name: /category/i }));
    await userEvent.click(await screen.findByRole("option", { name: "Tools" }));
    await userEvent.click(screen.getByRole("button", { name: /save|create/i }));
    await waitFor(() => expect(m.createAsset).toHaveBeenCalled());
    expect(m.createAsset.mock.calls[0][0]).toMatchObject({ tracking: "counted", quantity: 10 });
  });

  it("switching back to Individual resets quantity to 1 in the payload", async () => {
    renderForm("/assets/new");
    await userEvent.type(await screen.findByTestId("asset-form-name"), "Drone");
    await userEvent.click(screen.getByText("Counted"));
    const qty = await screen.findByTestId("asset-form-quantity");
    await userEvent.clear(qty);
    await userEvent.type(qty, "5");
    await userEvent.click(screen.getByText("Individual"));
    expect(screen.queryByTestId("asset-form-quantity")).toBeNull();

    await userEvent.click(screen.getByRole("textbox", { name: /category/i }));
    await userEvent.click(await screen.findByRole("option", { name: "Tools" }));
    await userEvent.click(screen.getByRole("button", { name: /save|create/i }));
    await waitFor(() => expect(m.createAsset).toHaveBeenCalled());
    expect(m.createAsset.mock.calls[0][0]).toMatchObject({ tracking: "individual", quantity: 1 });
  });

  it("hides the Tracking choice when Consumable is on", async () => {
    renderForm("/assets/new");
    await screen.findByTestId("asset-form-name");
    expect(screen.getByTestId("asset-form-tracking")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("switch", { name: /consumable/i }));
    expect(screen.queryByTestId("asset-form-tracking")).toBeNull();
    expect(screen.queryByTestId("asset-form-quantity")).toBeNull();
  });

  it("edit: loads counted asset, sends updated quantity, shows server field errors", async () => {
    m.getAsset.mockResolvedValue(makeAsset({ tracking: "counted", quantity: 10 }));
    m.updateAsset.mockRejectedValueOnce(
      new ApiError({
        type: "about:blank",
        title: "Bad Request",
        status: 400,
        errors: { quantity: ["Too many."] },
      }),
    );
    renderForm("/assets/10/edit");
    const qty = await screen.findByTestId("asset-form-quantity");
    expect(qty).toHaveValue("10");
    await userEvent.clear(qty);
    await userEvent.type(qty, "12");
    await userEvent.click(screen.getByRole("button", { name: /save|update/i }));
    await waitFor(() => expect(m.updateAsset).toHaveBeenCalled());
    expect(m.updateAsset.mock.calls[0][1]).toMatchObject({ tracking: "counted", quantity: 12 });
    expect(await screen.findByText("Too many.")).toBeInTheDocument();
  });
});
