import { describe, expect, it, vi, beforeEach } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { renderWithProviders } from "../../test/render";
import { EmailSettingsScreen } from "./EmailSettingsScreen";
import { api } from "../../api/client";
import type { EmailSettings, Me } from "../../api/types";

vi.mock("../../api/client", async () => {
  const actual = await vi.importActual<typeof import("../../api/client")>("../../api/client");
  return {
    ...actual,
    api: {
      getEmailSettings: vi.fn(),
      updateEmailSettings: vi.fn(),
    },
  };
});

// `useAuth()` provides `me`; this screen gates entirely on
// `hasPermission(me, TENANT_MANAGE)`, so each test controls `me` via this
// mock rather than wiring a real `AuthProvider` + mocked `api.me()` (same
// approach as `MyItemsScreen.test.tsx`).
let mockMe: Me | null = null;
vi.mock("../../hooks/useAuth", () => ({
  useAuth: () => ({
    status: "authenticated",
    me: mockMe,
    error: null,
    login: vi.fn(),
    logout: vi.fn(),
    refresh: vi.fn(),
  }),
}));

const mockedApi = vi.mocked(api);

function makeMe(overrides: Partial<Me> = {}): Me {
  return {
    id: 1,
    email: "admin@example.test",
    name: "Admin",
    tenant: { id: 1, name: "T", slug: "t" },
    memberships: [],
    permissions: [],
    project_permissions: {},
    ...overrides,
  } as Me;
}

function makeSettings(overrides: Partial<EmailSettings> = {}): EmailSettings {
  return {
    provider: "console",
    sender_email: "",
    reply_to: "",
    api_key_last4: "",
    has_api_key: false,
    updated_at: "2026-07-01T00:00:00Z",
    ...overrides,
  };
}

function renderScreen() {
  return renderWithProviders(<EmailSettingsScreen />);
}

beforeEach(() => {
  mockMe = null;
  mockedApi.getEmailSettings.mockReset();
  mockedApi.updateEmailSettings.mockReset();
});

describe("EmailSettingsScreen", () => {
  it("renders a not-authorized state and never calls the API for a user without tenant.manage", async () => {
    mockMe = makeMe({ permissions: [] });

    renderScreen();

    expect(await screen.findByTestId("email-settings-forbidden")).toBeInTheDocument();
    expect(mockedApi.getEmailSettings).not.toHaveBeenCalled();
  });

  it("shows the masked last-4 hint and leaves the raw key input blank when a key is already stored", async () => {
    mockMe = makeMe({ permissions: ["tenant.manage"] });
    mockedApi.getEmailSettings.mockResolvedValue(
      makeSettings({ provider: "brevo", has_api_key: true, api_key_last4: "1234" }),
    );

    renderScreen();

    await waitFor(() => expect(mockedApi.getEmailSettings).toHaveBeenCalled());
    expect(await screen.findByText(/1234/)).toBeInTheDocument();

    const keyInput = screen.getByLabelText("Brevo API key") as HTMLInputElement;
    expect(keyInput.value).toBe("");
  });

  it("submitting without touching the API key field omits api_key from the update call", async () => {
    mockMe = makeMe({ permissions: ["tenant.manage"] });
    mockedApi.getEmailSettings.mockResolvedValue(
      makeSettings({
        provider: "brevo",
        sender_email: "old@example.test",
        has_api_key: true,
        api_key_last4: "1234",
      }),
    );
    mockedApi.updateEmailSettings.mockResolvedValue(
      makeSettings({
        provider: "brevo",
        sender_email: "new@example.test",
        has_api_key: true,
        api_key_last4: "1234",
      }),
    );

    renderScreen();
    await waitFor(() => expect(mockedApi.getEmailSettings).toHaveBeenCalled());

    const user = userEvent.setup();
    const senderInput = await screen.findByLabelText("Sender email");
    await user.clear(senderInput);
    await user.type(senderInput, "new@example.test");
    await user.click(screen.getByTestId("email-settings-submit"));

    await waitFor(() => expect(mockedApi.updateEmailSettings).toHaveBeenCalledTimes(1));
    const payload = mockedApi.updateEmailSettings.mock.calls[0][0];
    expect(payload.sender_email).toBe("new@example.test");
    expect(payload).not.toHaveProperty("api_key");
  });

  it("submitting after checking 'Clear stored key' calls the API with api_key: ''", async () => {
    mockMe = makeMe({ permissions: ["tenant.manage"] });
    mockedApi.getEmailSettings.mockResolvedValue(
      makeSettings({
        provider: "brevo",
        sender_email: "keep@example.test",
        has_api_key: true,
        api_key_last4: "1234",
      }),
    );
    mockedApi.updateEmailSettings.mockResolvedValue(
      makeSettings({
        provider: "brevo",
        sender_email: "keep@example.test",
        has_api_key: false,
        api_key_last4: "",
      }),
    );

    renderScreen();
    await waitFor(() => expect(mockedApi.getEmailSettings).toHaveBeenCalled());

    const user = userEvent.setup();
    await user.click(await screen.findByTestId("email-settings-clear-key-toggle"));
    await user.click(screen.getByTestId("email-settings-submit"));

    await waitFor(() => expect(mockedApi.updateEmailSettings).toHaveBeenCalledTimes(1));
    const payload = mockedApi.updateEmailSettings.mock.calls[0][0];
    expect(payload.api_key).toBe("");
  });
});
