import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
import pkg from "./package.json";

/**
 * Separate from `vite.config.ts` (rather than merging `test` into it) so the
 * PWA plugin's build-time asset scanning/service-worker generation never
 * runs under the test runner — it isn't needed for unit/component tests and
 * has no bearing on them.
 */
export default defineConfig({
  plugins: [react()],
  // Mirrors `vite.config.ts`'s `define` — `AppLayout`'s footer reads the
  // build-time-inlined `__APP_VERSION__` global, which doesn't exist under
  // vitest without this (every test that renders `AppLayout` would fail
  // with `ReferenceError: __APP_VERSION__ is not defined`).
  define: {
    __APP_VERSION__: JSON.stringify(pkg.version),
  },
  test: {
    environment: "jsdom",
    setupFiles: ["./src/test/setup.ts"],
    css: false,
    restoreMocks: true,
  },
});
