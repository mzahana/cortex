import { describe, expect, it, vi } from "vitest";
import { screen } from "@testing-library/react";
import { renderWithProviders } from "../../test/render";
import { AssetCardRow, AssetTableRow } from "./AssetRows";
import { makeAsset } from "./countedAssets.testutil";

const lookups = { categoryName: "Tools", locationName: "", projectName: "", onOpen: vi.fn() };

describe("asset rows count badge", () => {
  it("shows x N on card and table rows for counted assets", () => {
    const a = makeAsset({ tracking: "counted", quantity: 10 });
    renderWithProviders(
      <>
        <AssetCardRow asset={a} {...lookups} />
        <AssetTableRow asset={a} {...lookups} />
      </>,
    );
    const badges = screen.getAllByTestId("asset-count-badge");
    expect(badges).toHaveLength(2);
    badges.forEach((b) => expect(b).toHaveTextContent("×10"));
  });

  it("shows no badge for individual assets", () => {
    renderWithProviders(<AssetCardRow asset={makeAsset()} {...lookups} />);
    expect(screen.queryByTestId("asset-count-badge")).toBeNull();
  });
});
