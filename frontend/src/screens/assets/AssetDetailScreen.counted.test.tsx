import { beforeEach, describe, expect, it, vi } from "vitest";
import { screen } from "@testing-library/react";
import { render } from "@testing-library/react";
import { MantineProvider } from "@mantine/core";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { theme } from "../../theme";
import { api } from "../../api/client";
import { AssetDetailScreen } from "./AssetDetailScreen";
import { makeAsset, ME } from "./countedAssets.testutil";

vi.mock("../../api/client", async () => {
  const actual = await vi.importActual<typeof import("../../api/client")>("../../api/client");
  return {
    ...actual,
    api: {
      getAsset: vi.fn(),
      getCategory: vi.fn(),
      listCategoryFields: vi.fn(),
      getLocation: vi.fn(),
      getProject: vi.fn(),
      listProjects: vi.fn(),
      listCheckouts: vi.fn(),
      listStock: vi.fn(),
      listReservations: vi.fn(),
    },
  };
});
vi.mock("../../hooks/useAuth", () => ({ useAuth: () => ({ status: "authenticated", me: ME }) }));
vi.mock("../../layout/AppLayout", () => ({
  AppLayout: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
}));

beforeEach(() => {
  const m = vi.mocked(api);
  const page = { count: 0, next: null, previous: null, results: [] };
  m.getCategory.mockResolvedValue(null as never);
  m.listCategoryFields.mockResolvedValue([]);
  m.getLocation.mockResolvedValue(null as never);
  m.getProject.mockResolvedValue(null as never);
  m.listProjects.mockResolvedValue(page);
  m.listCheckouts.mockResolvedValue(page);
  m.listStock.mockResolvedValue(page);
  m.listReservations.mockResolvedValue(page);
});

function renderDetail() {
  return render(
    <MantineProvider theme={theme}>
      <MemoryRouter initialEntries={["/assets/10"]}>
        <Routes>
          <Route path="/assets/:id" element={<AssetDetailScreen />} />
        </Routes>
      </MemoryRouter>
    </MantineProvider>,
  );
}

describe("AssetDetailScreen counted assets", () => {
  it("shows Counted · Quantity and disables Reserve / Check out", async () => {
    vi.mocked(api.getAsset).mockResolvedValue(makeAsset({ tracking: "counted", quantity: 10 }));
    renderDetail();
    expect(await screen.findByTestId("asset-counted-line")).toHaveTextContent("Counted · Quantity 10");
    expect(screen.queryByTestId("reserve-action")).toBeNull();
    expect(screen.queryByTestId("checkout-action")).toBeNull();
    expect(screen.getByRole("button", { name: "Reserve" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Check out" })).toBeDisabled();
  });

  it("keeps Reserve / Check out for individual assets", async () => {
    vi.mocked(api.getAsset).mockResolvedValue(makeAsset());
    renderDetail();
    expect(await screen.findByTestId("reserve-action")).toBeEnabled();
    expect(screen.getByTestId("checkout-action")).toBeEnabled();
    expect(screen.queryByTestId("asset-counted-line")).toBeNull();
  });
});
