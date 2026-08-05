import { describe, expect, it, vi, beforeEach } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { renderWithProviders } from "../../test/render";
import { RolesPermissionsPanel } from "./RolesPermissionsPanel";
import { UserPermissionsModal } from "./UserPermissionsModal";
import { api } from "../../api/client";
import { ApiError } from "../../api/problem";
import type { PermissionCatalogEntry, Role, UserPermissions } from "../../api/types";

vi.mock("../../api/client", async () => {
  const actual = await vi.importActual<typeof import("../../api/client")>("../../api/client");
  return {
    ...actual,
    api: {
      listRoles: vi.fn(),
      listPermissionCatalog: vi.fn(),
      updateRole: vi.fn(),
      resetRole: vi.fn(),
      getUserPermissions: vi.fn(),
      updateUserPermissions: vi.fn(),
    },
  };
});

const mockedApi = vi.mocked(api);

/** Mantine's `Checkbox` forwards extra props (including `data-testid`) to the
 * `<input>` itself, so the testid IS the checkbox — unlike `SegmentedControl`
 * below, where it lands on the wrapper and the radios live inside. */
function checkbox(key: string): HTMLInputElement {
  return screen.getByTestId(`perm-${key}`) as HTMLInputElement;
}

const CATALOG: PermissionCatalogEntry[] = [
  { id: 1, key: "asset.view", label: "View inventory / assets", group: "asset" },
  { id: 2, key: "asset.create", label: "Add asset", group: "asset" },
  { id: 3, key: "category.manage", label: "Manage categories & custom fields", group: "category" },
];

function makeRole(overrides: Partial<Role> = {}): Role {
  return {
    id: 10,
    key: "project_lead",
    name: "Project Lead",
    is_system: true,
    is_customized: false,
    permission_keys: ["asset.view", "asset.create"],
    member_count: 2,
    ...overrides,
  };
}

describe("RolesPermissionsPanel", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockedApi.listPermissionCatalog.mockResolvedValue({ results: CATALOG });
    mockedApi.listRoles.mockResolvedValue({
      count: 1,
      next: null,
      previous: null,
      results: [makeRole()],
    });
  });

  it("renders the permission matrix with the role's current grants checked", async () => {
    renderWithProviders(<RolesPermissionsPanel />);

    await waitFor(() => expect(screen.getByTestId("perm-asset.view")).toBeInTheDocument());
    expect(checkbox("asset.view")).toBeChecked();
    expect(checkbox("asset.create")).toBeChecked();
    expect(checkbox("category.manage")).not.toBeChecked();
  });

  it("sends the FULL permission set on save — an added box and the existing ones", async () => {
    /** The motivating case: granting Project Lead `category.manage`. The PATCH
     * body must carry every key that should survive, because the server
     * treats `permission_keys` as a wholesale replacement. */
    mockedApi.updateRole.mockResolvedValue(
      makeRole({
        is_customized: true,
        permission_keys: ["asset.view", "asset.create", "category.manage"],
      }),
    );
    renderWithProviders(<RolesPermissionsPanel />);
    await waitFor(() => expect(screen.getByTestId("perm-category.manage")).toBeInTheDocument());

    await userEvent.click(checkbox("category.manage"));
    await userEvent.click(screen.getByTestId("save-role-permissions"));

    await waitFor(() => expect(mockedApi.updateRole).toHaveBeenCalled());
    const [roleId, payload] = mockedApi.updateRole.mock.calls[0];
    expect(roleId).toBe(10);
    expect([...(payload.permission_keys ?? [])].sort()).toEqual([
      "asset.create",
      "asset.view",
      "category.manage",
    ]);
  });

  it("unchecking a box revokes it (the key is absent from the saved set)", async () => {
    mockedApi.updateRole.mockResolvedValue(
      makeRole({ is_customized: true, permission_keys: ["asset.view"] }),
    );
    renderWithProviders(<RolesPermissionsPanel />);
    await waitFor(() => expect(screen.getByTestId("perm-asset.create")).toBeInTheDocument());

    await userEvent.click(checkbox("asset.create"));
    await userEvent.click(screen.getByTestId("save-role-permissions"));

    await waitFor(() => expect(mockedApi.updateRole).toHaveBeenCalled());
    expect(mockedApi.updateRole.mock.calls[0][1].permission_keys).toEqual(["asset.view"]);
  });

  it("offers 'Reset to defaults' only once a system role has been customized", async () => {
    renderWithProviders(<RolesPermissionsPanel />);
    await waitFor(() => expect(screen.getByTestId("role-select")).toBeInTheDocument());
    expect(screen.queryByText("Reset to defaults")).not.toBeInTheDocument();

    mockedApi.listRoles.mockResolvedValue({
      count: 1,
      next: null,
      previous: null,
      results: [makeRole({ is_customized: true })],
    });
    renderWithProviders(<RolesPermissionsPanel />);

    await waitFor(() => expect(screen.getAllByText("Reset to defaults").length).toBeGreaterThan(0));
  });

  it("surfaces the server's lockout guardrail message instead of crashing", async () => {
    mockedApi.updateRole.mockRejectedValue(
      new ApiError({
        type: "about:blank",
        title: "ValidationError",
        status: 400,
        detail: "This change would leave no active user able to administer the tenant.",
      }),
    );
    renderWithProviders(<RolesPermissionsPanel />);
    await waitFor(() => expect(screen.getByTestId("perm-asset.view")).toBeInTheDocument());

    await userEvent.click(checkbox("asset.view"));
    await userEvent.click(screen.getByTestId("save-role-permissions"));

    expect(await screen.findByTestId("roles-error")).toHaveTextContent(
      /no active user able to administer/i,
    );
  });
});

describe("UserPermissionsModal", () => {
  function makeUserPermissions(overrides: Partial<UserPermissions> = {}): UserPermissions {
    return {
      user: 7,
      user_email: "lead@example.test",
      role_permission_keys: ["asset.view"],
      overrides: {},
      effective_permission_keys: ["asset.view"],
      ...overrides,
    };
  }

  beforeEach(() => {
    vi.clearAllMocks();
    mockedApi.listPermissionCatalog.mockResolvedValue({ results: CATALOG });
    mockedApi.getUserPermissions.mockResolvedValue(makeUserPermissions());
  });

  it("PUTs only the non-inherit entries", async () => {
    mockedApi.updateUserPermissions.mockResolvedValue(
      makeUserPermissions({ overrides: { "category.manage": "grant" } }),
    );
    renderWithProviders(
      <UserPermissionsModal
        opened
        userId={7}
        userEmail="lead@example.test"
        onClose={() => {}}
      />,
    );
    await waitFor(() =>
      expect(screen.getByTestId("override-category.manage")).toBeInTheDocument(),
    );

    await userEvent.click(
      screen.getByTestId("override-category.manage").querySelector('input[value="grant"]')!,
    );
    await userEvent.click(screen.getByTestId("save-user-permissions"));

    await waitFor(() => expect(mockedApi.updateUserPermissions).toHaveBeenCalled());
    const [userId, overrides] = mockedApi.updateUserPermissions.mock.calls[0];
    expect(userId).toBe(7);
    // Only the changed key — every other permission stays on "inherit" and
    // must NOT be sent (sending it would materialize a pointless override).
    expect(overrides).toEqual({ "category.manage": "grant" });
  });

  it("seeds the controls from the user's existing overrides", async () => {
    mockedApi.getUserPermissions.mockResolvedValue(
      makeUserPermissions({ overrides: { "asset.view": "deny" }, effective_permission_keys: [] }),
    );
    renderWithProviders(
      <UserPermissionsModal opened userId={7} userEmail="lead@example.test" onClose={() => {}} />,
    );

    await waitFor(() => expect(screen.getByTestId("override-asset.view")).toBeInTheDocument());
    expect(
      screen.getByTestId("override-asset.view").querySelector('input[value="deny"]'),
    ).toBeChecked();
  });

  it("removing an override sends an empty map (back to role defaults)", async () => {
    mockedApi.getUserPermissions.mockResolvedValue(
      makeUserPermissions({ overrides: { "asset.view": "deny" } }),
    );
    mockedApi.updateUserPermissions.mockResolvedValue(makeUserPermissions());
    renderWithProviders(
      <UserPermissionsModal opened userId={7} userEmail="lead@example.test" onClose={() => {}} />,
    );
    await waitFor(() => expect(screen.getByTestId("override-asset.view")).toBeInTheDocument());

    await userEvent.click(
      screen.getByTestId("override-asset.view").querySelector('input[value="inherit"]')!,
    );
    await userEvent.click(screen.getByTestId("save-user-permissions"));

    await waitFor(() => expect(mockedApi.updateUserPermissions).toHaveBeenCalled());
    expect(mockedApi.updateUserPermissions.mock.calls[0][1]).toEqual({});
  });
});
