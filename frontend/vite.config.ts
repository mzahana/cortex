import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Same-origin `/api/v1` in production (nginx proxies it to `web`). In local
// `vite dev` (outside docker), proxy `/api` to the backend so the typed
// client's relative base path works unchanged in both modes.
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/api": {
        target: "http://localhost:8000",
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: "dist",
    sourcemap: true,
  },
});
