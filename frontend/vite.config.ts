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
    // T1.6 finding: Vite's default `assetsDir` ("assets") collides with the
    // new `/assets` and `/assets/:id` SPA routes at nginx's static-file
    // layer — `location /`'s `try_files $uri $uri/ /index.html` resolves a
    // direct navigation/refresh of `/assets` to the literal `dist/assets/`
    // directory (403, no autoindex) BEFORE ever falling back to
    // `index.html`, instead of routing to `AssetListScreen`. Renaming the
    // build's own asset output dir to something no top-level route will
    // ever be named avoids the clash entirely (verified against the real
    // nginx container while building this screen).
    assetsDir: "static-assets",
  },
});
