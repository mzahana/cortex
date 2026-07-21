import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { VitePWA } from "vite-plugin-pwa";

// Same-origin `/api/v1` in production (nginx proxies it to `web`). In local
// `vite dev` (outside docker), proxy `/api` to the backend so the typed
// client's relative base path works unchanged in both modes.
export default defineConfig({
  plugins: [
    react(),
    // T4.2: installable PWA shell + offline app-shell cache. `manifest:
    // false` because the manifest is a hand-authored static file
    // (`public/manifest.webmanifest`, linked from `index.html`) rather than
    // plugin-generated — keeps the single source of truth explicit. This is
    // deliberately the *shell* cache only: no runtimeCaching entries, so API
    // responses (`/api/**`) are never intercepted/cached by the service
    // worker. Offline write-queueing is out of scope (Phase 3).
    VitePWA({
      manifest: false,
      registerType: "autoUpdate",
      injectRegister: "auto",
      includeAssets: ["icons/icon.svg"],
      workbox: {
        // Precache the built shell only (JS/CSS/HTML/icons). No
        // runtimeCaching config is added on purpose: API calls under
        // `/api/**` must always hit the network, never the SW cache.
        globPatterns: ["**/*.{js,css,html,svg,webmanifest}"],
        navigateFallback: "/index.html",
        // T4.5: an `<a download>` click for a generated PDF (label sheets,
        // Attachments) is dispatched as a navigation request, so `/media/`
        // must be denylisted too -- otherwise Workbox's NavigationRoute
        // intercepts it and "downloads" the cached index.html instead of
        // the actual file.
        navigateFallbackDenylist: [/^\/api\//, /^\/media\//],
      },
    }),
  ],
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
