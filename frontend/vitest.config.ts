import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

/**
 * Separate from `vite.config.ts` (rather than merging `test` into it) so the
 * PWA plugin's build-time asset scanning/service-worker generation never
 * runs under the test runner — it isn't needed for unit/component tests and
 * has no bearing on them.
 */
export default defineConfig({
  plugins: [react()],
  test: {
    environment: "jsdom",
    setupFiles: ["./src/test/setup.ts"],
    css: false,
    restoreMocks: true,
  },
});
