import { describe, expect, it, vi, beforeEach } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import { renderWithProviders } from "../../test/render";
import { ProjectsListScreen } from "./ProjectsListScreen";
import { api } from "../../api/client";
import type { Me, Paginated, Project } from "../../api/types";

vi.mock("../../api/client", async () => {
  const actual = await vi.importActual<typeof import("../../api/client")>("../../api/client");
  return {
    ...actual,
    api: {
      listProjects: vi.fn(),
      listUsers: vi.fn(),
      deleteProject: vi.fn(),
    },
  };
});

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
    email: "user@example.test",
    name: "Test User",
    tenant: { id: 1, name: "T", slug: "t" },
    memberships: [],
    permissions: [],
    project_permissions: {},
    ...overrides,
  } as Me;
}

function makeProject(overrides: Partial<Project> = {}): Project {
  return {
    id: 1,
    name: "Robotics Grant",
    code: "NSF-1",
    lead_user: null,
    is_active: true,
    funding_source: "external",
    sponsor: "NSF",
    start_date: null,
    end_date: null,
    budget_total: "10000.00",
    currency: "USD",
    status: "active",
    description: "",
    created_at: "2026-01-01T00:00:00Z",
    ...overrides,
  };
}

function paginated<T>(results: T[]): Paginated<T> {
  return { count: results.length, next: null, previous: null, results };
}

beforeEach(() => {
  mockMe = null;
  mockedApi.listProjects.mockReset();
  mockedApi.listUsers.mockReset();
  mockedApi.deleteProject.mockReset();
  mockedApi.listUsers.mockResolvedValue(paginated([]));
});

describe("ProjectsListScreen", () => {
  it("renders a locked budget affordance (not $0) for a project whose financials are redacted", async () => {
    mockMe = makeMe({ permissions: [], project_permissions: { "1": ["project.view"] } });
    mockedApi.listProjects.mockResolvedValue(paginated([makeProject({ budget_total: null })]));

    renderWithProviders(<ProjectsListScreen />);

    await waitFor(() => expect(screen.getByTestId("projects-table")).toBeInTheDocument());
    expect(screen.getByTestId("project-budget-locked-1")).toBeInTheDocument();
    expect(screen.queryByTestId("project-budget-1")).not.toBeInTheDocument();
    expect(screen.queryByText("USD 0.00")).not.toBeInTheDocument();
  });

  it("shows a real budget figure for an authorized caller, and hides the New project button for non-admins", async () => {
    mockMe = makeMe({ permissions: [], project_permissions: { "1": ["project.view", "expense.view"] } });
    mockedApi.listProjects.mockResolvedValue(paginated([makeProject()]));

    renderWithProviders(<ProjectsListScreen />);

    await waitFor(() => expect(screen.getByTestId("project-budget-1")).toBeInTheDocument());
    expect(screen.getByTestId("project-budget-1")).toHaveTextContent("USD 10000.00");
    expect(screen.queryByTestId("new-project-button")).not.toBeInTheDocument();
  });

  it("shows the New project button and delete action for tenant.manage admins", async () => {
    mockMe = makeMe({ permissions: ["tenant.manage"] });
    mockedApi.listProjects.mockResolvedValue(paginated([makeProject()]));

    renderWithProviders(<ProjectsListScreen />);

    expect(screen.getByTestId("new-project-button")).toBeInTheDocument();
    await waitFor(() => expect(screen.getByTestId("project-row-1")).toBeInTheDocument());
    expect(screen.getByLabelText("Delete Robotics Grant")).toBeInTheDocument();
  });
});
