import { beforeEach, describe, expect, it, vi } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { renderWithProviders } from "../../test/render";
import { api } from "../../api/client";
import { AssetListView } from "./AssetListView";
import { makeAsset, ME } from "./countedAssets.testutil";

vi.mock("../../api/client", async () => {
  const actual = await vi.importActual<typeof import("../../api/client")>("../../api/client");
  return {
    ...actual,
    api: {
      listAllCategories: vi.fn(),
      listAllLocations: vi.fn(),
      listAllProjects: vi.fn(),
      listAllTags: vi.fn(),
      exportAssetsCsvUrl: vi.fn(),
    },
  };
});
vi.mock("../../hooks/useAuth", () => ({ useAuth: () => ({ status: "authenticated", me: ME }) }));
// jsdom has no layout, so the real virtualizer renders zero rows.
vi.mock("@tanstack/react-virtual", () => ({
  useVirtualizer: ({ count }: { count: number }) => ({
    getVirtualItems: () =>
      Array.from({ length: count }, (_, index) => ({ index, key: index, start: index * 80, size: 80 })),
    getTotalSize: () => count * 80,
  }),
}));

beforeEach(() => {
  const m = vi.mocked(api);
  m.listAllCategories.mockResolvedValue([]);
  m.listAllLocations.mockResolvedValue([]);
  m.listAllProjects.mockResolvedValue([]);
  m.listAllTags.mockResolvedValue([]);
  m.exportAssetsCsvUrl.mockReturnValue("/x.csv");
});

describe("AssetListView tracking filter", () => {
  it("shows the count and sends ?tracking= to the server", async () => {
    const fetchAssets = vi.fn().mockResolvedValue({
      count: 1,
      next: null,
      previous: null,
      results: [makeAsset({ tracking: "counted", quantity: 10 })],
    });
    renderWithProviders(<AssetListView fetchAssets={fetchAssets} />);
    expect((await screen.findAllByTestId("asset-count-badge"))[0]).toHaveTextContent("×10");
    expect(fetchAssets.mock.calls[0][0].tracking).toBeUndefined();

    await userEvent.click(screen.getByRole("textbox", { name: "Filter by tracking" }));
    await userEvent.click(await screen.findByRole("option", { name: "Counted" }));
    await waitFor(() =>
      expect(fetchAssets.mock.calls.some(([p]) => p.tracking === "counted")).toBe(true),
    );
  });
});
