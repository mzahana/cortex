import { describe, expect, it, vi, beforeEach } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { renderWithProviders } from "../../test/render";
import { BrandingScreen } from "./BrandingScreen";
import { api, ApiError } from "../../api/client";
import type { Me, TenantBranding } from "../../api/types";

vi.mock("../../api/client", async () => {
  const actual = await vi.importActual<typeof import("../../api/client")>("../../api/client");
  return {
    ...actual,
    api: {
      tenantBranding: vi.fn(),
      uploadTenantLogo: vi.fn(),
      deleteTenantLogo: vi.fn(),
    },
  };
});

// Same `useAuth()` mocking approach as `SessionSettingsScreen.test.tsx`: the
// screen's write affordances gate on `hasPermission(me, TENANT_MANAGE)`.
let mockMe: Me | null = null;
const refresh = vi.fn();
vi.mock("../../hooks/useAuth", () => ({
  useAuth: () => ({
    status: "authenticated",
    me: mockMe,
    error: null,
    login: vi.fn(),
    logout: vi.fn(),
    refresh,
  }),
}));

const mockedApi = vi.mocked(api);

function makeMe(permissions: string[]): Me {
  return {
    id: 1,
    email: "admin@example.test",
    name: "Admin",
    display_name: "Admin",
    tenant: { id: 1, name: "Acme Robotics Lab", slug: "acme", logo_url: null },
    memberships: [],
    permissions,
    project_permissions: {},
  } as Me;
}

function makeBranding(overrides: Partial<TenantBranding> = {}): TenantBranding {
  return {
    id: 1,
    slug: "acme",
    name: "Acme Robotics Lab",
    logo_url: null,
    logo_filename: "",
    logo_updated_at: null,
    ...overrides,
  };
}

function renderScreen() {
  return renderWithProviders(<BrandingScreen />);
}

describe("BrandingScreen", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockMe = makeMe(["tenant.manage"]);
  });

  it("shows the lab name and an empty state when no logo is set", async () => {
    mockedApi.tenantBranding.mockResolvedValue(makeBranding());
    renderScreen();

    // The shell's sidebar shows the lab name too, so scope to the card.
    expect(await screen.findByTestId("branding-no-logo")).toBeInTheDocument();
    expect(screen.getAllByText("Acme Robotics Lab").length).toBeGreaterThan(0);
    expect(screen.getByTestId("branding-upload")).toHaveTextContent("Upload logo");
  });

  it("uploads a chosen PNG and refreshes the session so the chrome updates", async () => {
    mockedApi.tenantBranding.mockResolvedValue(makeBranding());
    mockedApi.uploadTenantLogo.mockResolvedValue(
      makeBranding({ logo_url: "/media/tenant-logos/1/abc_logo.png", logo_filename: "logo.png" }),
    );
    renderScreen();
    await screen.findByTestId("branding-upload");

    const file = new File(["x"], "logo.png", { type: "image/png" });
    await userEvent.upload(screen.getByTestId("branding-file-input"), file);

    await waitFor(() => expect(mockedApi.uploadTenantLogo).toHaveBeenCalledWith(file));
    expect(refresh).toHaveBeenCalled();
    expect(await screen.findByTestId("branding-logo-preview")).toBeInTheDocument();
    expect(screen.getByTestId("branding-success")).toBeInTheDocument();
  });

  it("only offers the allowed image types in the file picker", async () => {
    // The client-side guard in `handleFile` can't be driven through
    // `userEvent.upload` — it honours the input's `accept` filter and drops a
    // disallowed file before any change event fires, which is precisely the
    // behavior asserted here. The server-side allowlist (the real boundary)
    // is covered by `apps.tenancy.tests.test_tenant_logo_api` and, for its
    // error surfacing, by the next test.
    mockedApi.tenantBranding.mockResolvedValue(makeBranding());
    renderScreen();
    await screen.findByTestId("branding-upload");

    expect(screen.getByTestId("branding-file-input")).toHaveAttribute(
      "accept",
      "image/png,image/jpeg,image/webp",
    );
  });

  it("surfaces the server's field error when the upload is rejected", async () => {
    mockedApi.tenantBranding.mockResolvedValue(makeBranding());
    mockedApi.uploadTenantLogo.mockRejectedValue(
      new ApiError({
        type: "about:blank",
        title: "ValidationError",
        status: 400,
        detail: "Validation failed.",
        errors: { file: "Logo must be a PNG, JPEG, or WebP image." },
      }),
    );
    renderScreen();
    await screen.findByTestId("branding-upload");

    await userEvent.upload(
      screen.getByTestId("branding-file-input"),
      new File(["x"], "logo.png", { type: "image/png" }),
    );

    expect(await screen.findByTestId("branding-error")).toHaveTextContent(
      "Logo must be a PNG, JPEG, or WebP image.",
    );
  });

  it("removes an existing logo", async () => {
    mockedApi.tenantBranding.mockResolvedValue(
      makeBranding({ logo_url: "/media/tenant-logos/1/abc_logo.png", logo_filename: "logo.png" }),
    );
    mockedApi.deleteTenantLogo.mockResolvedValue(makeBranding());
    renderScreen();

    await userEvent.click(await screen.findByTestId("branding-remove"));

    await waitFor(() => expect(mockedApi.deleteTenantLogo).toHaveBeenCalled());
    expect(await screen.findByTestId("branding-no-logo")).toBeInTheDocument();
  });

  it("hides the upload/remove controls from a user without tenant.manage", async () => {
    mockMe = makeMe([]);
    mockedApi.tenantBranding.mockResolvedValue(
      makeBranding({ logo_url: "/media/tenant-logos/1/abc_logo.png" }),
    );
    renderScreen();

    expect(await screen.findByTestId("branding-logo-preview")).toBeInTheDocument();
    expect(screen.queryByTestId("branding-upload")).not.toBeInTheDocument();
    expect(screen.queryByTestId("branding-remove")).not.toBeInTheDocument();
  });
});
