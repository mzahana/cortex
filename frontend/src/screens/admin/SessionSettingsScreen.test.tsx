import { describe, expect, it, vi, beforeEach } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { renderWithProviders } from "../../test/render";
import { SessionSettingsScreen } from "./SessionSettingsScreen";
import { api, ApiError } from "../../api/client";
import type { Me, SessionSettings } from "../../api/types";

vi.mock("../../api/client", async () => {
  const actual = await vi.importActual<typeof import("../../api/client")>("../../api/client");
  return {
    ...actual,
    api: {
      getSessionSettings: vi.fn(),
      updateSessionSettings: vi.fn(),
    },
  };
});

// `useAuth()` provides `me`; this screen gates entirely on
// `hasPermission(me, TENANT_MANAGE)`, so each test controls `me` via this
// mock rather than wiring a real `AuthProvider` + mocked `api.me()` (same
// approach as `EmailSettingsScreen.test.tsx`).
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

function makeSettings(overrides: Partial<SessionSettings> = {}): SessionSettings {
  return {
    idle_timeout_minutes: 60,
    absolute_timeout_hours: 24,
    updated_at: "2026-07-01T00:00:00Z",
    ...overrides,
  };
}

function renderScreen() {
  return renderWithProviders(<SessionSettingsScreen />);
}

beforeEach(() => {
  mockMe = null;
  mockedApi.getSessionSettings.mockReset();
  mockedApi.updateSessionSettings.mockReset();
});

describe("SessionSettingsScreen", () => {
  it("renders a not-authorized state and never calls the API for a user without tenant.manage", async () => {
    mockMe = makeMe({ permissions: [] });

    renderScreen();

    expect(await screen.findByTestId("session-settings-forbidden")).toBeInTheDocument();
    expect(mockedApi.getSessionSettings).not.toHaveBeenCalled();
  });

  it("loads and displays the current settings for an admin", async () => {
    mockMe = makeMe({ permissions: ["tenant.manage"] });
    mockedApi.getSessionSettings.mockResolvedValue(
      makeSettings({ idle_timeout_minutes: 45, absolute_timeout_hours: 12 }),
    );

    renderScreen();

    await waitFor(() => expect(mockedApi.getSessionSettings).toHaveBeenCalled());
    const idleInput = (await screen.findByTestId("session-settings-idle-minutes")) as HTMLInputElement;
    const absoluteInput = screen.getByTestId(
      "session-settings-absolute-hours",
    ) as HTMLInputElement;
    expect(idleInput.value).toBe("45");
    expect(absoluteInput.value).toBe("12");
  });

  it("blocks submission with a client-side validation error when idle minutes is out of bounds", async () => {
    mockMe = makeMe({ permissions: ["tenant.manage"] });
    mockedApi.getSessionSettings.mockResolvedValue(makeSettings());

    renderScreen();
    await waitFor(() => expect(mockedApi.getSessionSettings).toHaveBeenCalled());

    const user = userEvent.setup();
    const idleInput = await screen.findByTestId("session-settings-idle-minutes");
    await user.clear(idleInput);
    await user.type(idleInput, "4");
    await user.click(screen.getByTestId("session-settings-submit"));

    expect(await screen.findByText(/Must be between 5 and 480 minutes/)).toBeInTheDocument();
    expect(mockedApi.updateSessionSettings).not.toHaveBeenCalled();
  });

  it("blocks submission with a client-side validation error when absolute hours is out of bounds", async () => {
    mockMe = makeMe({ permissions: ["tenant.manage"] });
    mockedApi.getSessionSettings.mockResolvedValue(makeSettings());

    renderScreen();
    await waitFor(() => expect(mockedApi.getSessionSettings).toHaveBeenCalled());

    const user = userEvent.setup();
    const absoluteInput = await screen.findByTestId("session-settings-absolute-hours");
    await user.clear(absoluteInput);
    await user.type(absoluteInput, "169");
    await user.click(screen.getByTestId("session-settings-submit"));

    expect(await screen.findByText(/Must be between 1 and 168 hours/)).toBeInTheDocument();
    expect(mockedApi.updateSessionSettings).not.toHaveBeenCalled();
  });

  it("saves successfully and updates the displayed values", async () => {
    mockMe = makeMe({ permissions: ["tenant.manage"] });
    mockedApi.getSessionSettings.mockResolvedValue(
      makeSettings({ idle_timeout_minutes: 60, absolute_timeout_hours: 24 }),
    );
    mockedApi.updateSessionSettings.mockResolvedValue(
      makeSettings({ idle_timeout_minutes: 90, absolute_timeout_hours: 48 }),
    );

    renderScreen();
    await waitFor(() => expect(mockedApi.getSessionSettings).toHaveBeenCalled());

    const user = userEvent.setup();
    const idleInput = await screen.findByTestId("session-settings-idle-minutes");
    await user.clear(idleInput);
    await user.type(idleInput, "90");
    const absoluteInput = screen.getByTestId("session-settings-absolute-hours");
    await user.clear(absoluteInput);
    await user.type(absoluteInput, "48");
    await user.click(screen.getByTestId("session-settings-submit"));

    await waitFor(() => expect(mockedApi.updateSessionSettings).toHaveBeenCalledTimes(1));
    const payload = mockedApi.updateSessionSettings.mock.calls[0][0];
    expect(payload).toEqual({ idle_timeout_minutes: 90, absolute_timeout_hours: 48 });

    expect(await screen.findByTestId("session-settings-saved")).toBeInTheDocument();
    const idleInputAfter = screen.getByTestId("session-settings-idle-minutes") as HTMLInputElement;
    expect(idleInputAfter.value).toBe("90");
  });

  it("surfaces a server-side validation error on save", async () => {
    mockMe = makeMe({ permissions: ["tenant.manage"] });
    mockedApi.getSessionSettings.mockResolvedValue(makeSettings());
    mockedApi.updateSessionSettings.mockRejectedValue(
      new ApiError({
        type: "about:blank",
        title: "Bad Request",
        status: 400,
        detail: "Validation failed.",
        errors: {
          idle_timeout_minutes: ["Ensure this value is greater than or equal to 5."],
        },
      }),
    );

    renderScreen();
    await waitFor(() => expect(mockedApi.getSessionSettings).toHaveBeenCalled());

    const user = userEvent.setup();
    await user.click(screen.getByTestId("session-settings-submit"));

    await waitFor(() => expect(mockedApi.updateSessionSettings).toHaveBeenCalledTimes(1));
    expect(
      await screen.findByText(/Ensure this value is greater than or equal to 5\./),
    ).toBeInTheDocument();
  });
});
