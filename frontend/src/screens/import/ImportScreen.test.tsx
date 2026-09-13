import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { MantineProvider } from "@mantine/core";
import { theme } from "../../theme";
import { ImportScreen } from "./ImportScreen";
import { api } from "../../api/client";
import type { ImportJob, ImportReport, Me } from "../../api/types";

vi.mock("../../api/client", async () => {
  const actual =
    await vi.importActual<typeof import("../../api/client")>("../../api/client");
  return {
    ...actual,
    api: {
      createImport: vi.fn(),
      getImport: vi.fn(),
      commitImport: vi.fn(),
      importTemplateXlsxUrl: vi.fn(
        () => "/api/v1/exports/asset-import-template.xlsx",
      ),
    },
  };
});

vi.mock("../../hooks/useAuth", () => ({
  useAuth: () => ({
    status: "authenticated",
    me: {
      id: 1,
      email: "admin@example.test",
      name: "Admin",
      display_name: "Admin",
      tenant: { id: 1, name: "T", slug: "t", logo_url: null },
      memberships: [],
      permissions: ["import.run"],
      project_permissions: {},
    } as Me,
    error: null,
    login: vi.fn(),
    logout: vi.fn(),
    refresh: vi.fn(),
    logoutAll: vi.fn(),
  }),
}));

const mockedApi = vi.mocked(api);

function makeReport(overrides: Partial<ImportReport> = {}): ImportReport {
  return {
    resolved_mapping: { name: "name", category: "category" },
    rows: [],
    total_rows: 1,
    valid_count: 1,
    invalid_count: 0,
    missing_references: { category: [], location: [], project: [] },
    create_missing: [],
    created_references: { category: [], location: [], project: [] },
    on_duplicate: "reject",
    duplicate_count: 0,
    duplicate_rows: [],
    skipped_count: 0,
    ...overrides,
  };
}

function makeJob(report: ImportReport | null, overrides: Partial<ImportJob> = {}): ImportJob {
  return {
    id: 7,
    status: "dry_run_succeeded",
    source_filename: "assets.xlsx",
    mapping: report?.resolved_mapping ?? {},
    report,
    created_asset_ids: [],
    dry_run_job: { id: "job-1", status: "succeeded", error: "" },
    commit_job: null,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
    ...overrides,
  };
}

function renderScreen() {
  return render(
    <MantineProvider theme={theme} defaultColorScheme="light">
      <MemoryRouter initialEntries={["/import"]}>
        <ImportScreen />
      </MemoryRouter>
    </MantineProvider>,
  );
}

/** Pick a file and run the first dry-run, landing on the review step. */
async function uploadTo(report: ImportReport) {
  const user = userEvent.setup();
  mockedApi.createImport.mockResolvedValue(makeJob(report));
  const { container } = renderScreen();

  const fileInput = container.querySelector('input[type="file"]') as HTMLInputElement;
  await user.upload(fileInput, new File(["a,b"], "assets.xlsx"));
  await user.click(screen.getByTestId("import-upload-button"));
  await screen.findByTestId("import-review-step");
  return user;
}

beforeEach(() => {
  Object.values(mockedApi).forEach((fn) => {
    if (typeof fn === "function" && "mockReset" in fn)
      (fn as ReturnType<typeof vi.fn>).mockReset();
  });
  mockedApi.importTemplateXlsxUrl.mockReturnValue(
    "/api/v1/exports/asset-import-template.xlsx",
  );
});

describe("ImportScreen template download", () => {
  it("offers the blank template as a direct download link", () => {
    renderScreen();
    const link = screen.getByTestId("import-template-download");
    expect(link).toHaveAttribute(
      "href",
      "/api/v1/exports/asset-import-template.xlsx",
    );
  });

  it("says up front that importing never touches existing assets", () => {
    renderScreen();
    expect(
      screen.getByText(/never edits or removes assets already in Cortex/i),
    ).toBeInTheDocument();
  });
});

describe("ImportScreen duplicate handling", () => {
  const duplicateReport = makeReport({
    invalid_count: 1,
    valid_count: 0,
    duplicate_count: 1,
    duplicate_rows: [
      {
        row_number: 2,
        name: "Jetson Orin",
        category: "Compute",
        existing_asset_ids: [42],
        duplicate_of_row: null,
        skipped: false,
      },
    ],
    rows: [
      {
        row_number: 2,
        values: {
          name: "Jetson Orin",
          category: "Compute",
          location: null,
          project: null,
          tags: [],
          status: "available",
          condition: "",
          custom_field_values: {},
        },
        unresolved_references: {},
        duplicate_of_asset_ids: [42],
        duplicate_of_row: null,
        skipped: false,
        errors: { duplicate: "An asset named 'Jetson Orin' already exists." },
      },
    ],
  });

  it("shows the offending rows instead of silently importing them", async () => {
    await uploadTo(duplicateReport);

    const table = screen.getByTestId("import-duplicate-table");
    expect(within(table).getByText("Jetson Orin")).toBeInTheDocument();
    expect(within(table).getByText("1 existing asset(s)")).toBeInTheDocument();
    // The commit is blocked until the user decides.
    expect(screen.getByTestId("import-commit-button")).toBeDisabled();
  });

  it("names the colliding row for a duplicate inside the same file", async () => {
    await uploadTo(
      makeReport({
        invalid_count: 1,
        duplicate_count: 1,
        duplicate_rows: [
          {
            row_number: 3,
            name: "Jetson Orin",
            category: "Compute",
            existing_asset_ids: [],
            duplicate_of_row: 2,
            skipped: false,
          },
        ],
      }),
    );

    expect(
      within(screen.getByTestId("import-duplicate-table")).getByText(
        "row 2 of this file",
      ),
    ).toBeInTheDocument();
  });

  it("re-runs the dry-run with the chosen policy", async () => {
    const user = await uploadTo(duplicateReport);
    mockedApi.createImport.mockClear();

    await user.click(screen.getByRole("radio", { name: /Skip the 1 duplicate/i }));
    await user.click(screen.getByTestId("import-recheck-duplicates-button"));

    await waitFor(() => expect(mockedApi.createImport).toHaveBeenCalledTimes(1));
    const [, , createMissing, onDuplicate] = mockedApi.createImport.mock.calls[0];
    expect(createMissing).toEqual([]);
    expect(onDuplicate).toBe("skip");
  });

  it("carries the chosen policy through to the commit", async () => {
    const clean = makeReport({ duplicate_count: 1, duplicate_rows: duplicateReport.duplicate_rows });
    const user = await uploadTo(clean);
    mockedApi.commitImport.mockResolvedValue(
      makeJob(makeReport({ skipped_count: 1 }), { status: "committed" }),
    );

    await user.click(screen.getByRole("radio", { name: /Import them anyway/i }));
    await user.click(screen.getByTestId("import-commit-button"));

    await waitFor(() => expect(mockedApi.commitImport).toHaveBeenCalledTimes(1));
    expect(mockedApi.commitImport.mock.calls[0][3]).toBe("create");
  });

  it("reports what it skipped once the import lands", async () => {
    const user = await uploadTo(makeReport({ duplicate_count: 0 }));
    mockedApi.commitImport.mockResolvedValue(
      makeJob(makeReport({ skipped_count: 2 }), {
        status: "committed",
        created_asset_ids: [1],
      }),
    );

    await user.click(screen.getByTestId("import-commit-button"));

    expect(await screen.findByTestId("import-skipped-count")).toHaveTextContent(
      "2 duplicate row(s) skipped",
    );
  });
});

describe("ImportScreen missing references", () => {
  const missingReport = makeReport({
    invalid_count: 1,
    valid_count: 0,
    missing_references: { category: [], location: ["Rack 9", "Shelf B2"], project: [] },
  });

  it("lists the names that don't exist yet", async () => {
    await uploadTo(missingReport);

    const panel = screen.getByTestId("import-missing-refs");
    expect(within(panel).getByText(/Rack 9, Shelf B2/)).toBeInTheDocument();
    expect(screen.getByTestId("import-create-missing-location")).not.toBeChecked();
  });

  it("asks for nothing when every name already resolves", async () => {
    await uploadTo(makeReport());
    expect(screen.queryByTestId("import-missing-refs")).not.toBeInTheDocument();
  });

  it("re-runs the dry-run with the ticked kinds", async () => {
    const user = await uploadTo(missingReport);
    mockedApi.createImport.mockClear();

    await user.click(screen.getByTestId("import-create-missing-location"));
    await user.click(screen.getByTestId("import-recheck-create-missing-button"));

    await waitFor(() => expect(mockedApi.createImport).toHaveBeenCalledTimes(1));
    expect(mockedApi.createImport.mock.calls[0][2]).toEqual(["location"]);
  });

  it("carries the ticked kinds through to the commit", async () => {
    const user = await uploadTo(
      makeReport({
        missing_references: { category: [], location: ["Rack 9"], project: [] },
        create_missing: ["location"],
      }),
    );
    mockedApi.commitImport.mockResolvedValue(
      makeJob(makeReport({ created_references: { category: [], location: ["Rack 9"], project: [] } }), {
        status: "committed",
        created_asset_ids: [1],
      }),
    );

    await user.click(screen.getByTestId("import-create-missing-location"));
    await user.click(screen.getByTestId("import-commit-button"));

    await waitFor(() => expect(mockedApi.commitImport).toHaveBeenCalledTimes(1));
    expect(mockedApi.commitImport.mock.calls[0][2]).toEqual(["location"]);
    expect(await screen.findByTestId("import-created-refs")).toHaveTextContent("Rack 9");
  });
});
