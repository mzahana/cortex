import { describe, expect, it } from "vitest";

// Shared with the backend parity test
// (`backend/apps/imports/tests/test_core_targets_parity.py`).
import fixture from "../../../backend/apps/imports/tests/core_targets.json?raw";
import { IMPORT_CORE_TARGETS } from "./types";

describe("IMPORT_CORE_TARGETS", () => {
  it("matches the backend's core import targets", () => {
    const backend: string[] = JSON.parse(fixture);
    expect([...IMPORT_CORE_TARGETS].sort()).toEqual(backend);
  });

  it("offers tracking and quantity after status", () => {
    const i = IMPORT_CORE_TARGETS.indexOf("status");
    expect(IMPORT_CORE_TARGETS.slice(i + 1, i + 3)).toEqual(["tracking", "quantity"]);
  });
});
