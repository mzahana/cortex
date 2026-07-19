# M4 — Mobile scan, photo, labels (MVP)

**Goal:** the phone experience — installable PWA, camera QR scan → asset, camera photo
capture, and Avery-sheet label PDF generation. Can proceed in **parallel after M1**;
final F6 acceptance needs the HTTPS deploy (real-device test over the Tunnel).
**Dep:** M1 (+ HTTPS deploy for on-device test). **Effort:** M.
**Milestone exit:** F6 + F7 acceptance met on a phone over the Tunnel domain.
Refs: `docs/features.md` F6/F7, `docs/deployment.md` §3, `docs/api-and-ui.md`.

---

### T4.1 · → backend-engineer · deps: M1
**Do:** Scan resolver `GET /api/v1/resolve/{qr_token}` → returns the asset id/detail for
the stable `qr_token` (tenant-scoped; unknown/foreign token → 404). This is the endpoint
the scan flow and the printed QR both target.
**Files:** `backend/apps/assets/api.py`.
**Exit:** a valid token resolves to its asset within the perf budget (< 250 ms); a
cross-tenant token 404s.

### T4.2 · → pwa-scan-specialist · deps: T0.7
**Do:** PWA manifest (installable, add-to-home-screen) + a service worker caching the app
shell for offline load. Keep it simple — no offline write-queue (Phase 3).
**Files:** `frontend/public/manifest.webmanifest`, `frontend/src/sw.ts`,
`frontend/vite.config.ts` (PWA plugin).
**Exit:** the app is installable on a phone and loads its shell offline.

### T4.3 · → pwa-scan-specialist · deps: T4.1, T4.2
**Do:** Scan screen (primary FAB): `@zxing/browser` camera scan → extract token → call
`/resolve/{token}` → route to Asset Detail with quick check-in/out. Include a **manual
token/asset-ID entry fallback** (risk R5). Requires secure context (HTTPS/localhost).
**Files:** `frontend/src/screens/scan/*`.
**Exit:** scanning a code opens the exact asset with check-in/out offered; fallback works
when the camera is unavailable.

### T4.4 · → pwa-scan-specialist · deps: T1.2
**Do:** Camera photo capture → upload multipart to `POST /assets/{id}/attachments`;
upload in the background where possible; the photo appears on the asset within seconds.
**Files:** `frontend/src/screens/assets/PhotoCapture.tsx`.
**Exit:** F6 photo half: a captured photo attaches and renders on the asset quickly.

### T4.5 · → pwa-scan-specialist · deps: T1.2 · (backend Celery)
**Do:** Label PDF generation: `POST /api/v1/labels/generate` (body: asset ids + sheet
template) enqueues a **Celery** job; the worker renders QR codes with `segno` (encoding
each asset's stable `qr_token`/URL) laid out on selectable **Avery-style sheet templates**
via `WeasyPrint`; poll `GET /api/v1/jobs/{id}`; download the PDF. Human-readable name/ID/
category/location on each label. Enforce `label.generate` (scoped).
**Files:** `backend/apps/labels/` (tasks, templates), `frontend/src/screens/labels/*`.
**Exit:** F7: select N assets → print-ready Avery PDF.

### T4.6 · → qa-test-engineer + pwa-scan-specialist · deps: T4.3, T4.5 · **milestone gate**
**Do:** Close the loop: generate a label PDF → scan its QR (F6) → the exact asset opens.
On-device matrix: iOS Safari + Android Chrome over the Tunnel HTTPS domain (needs the M6
deploy, or an interim tunnel). Backend job/resolver covered by automated tests; the
camera round-trip is a documented manual device test. code-reviewer gates the diff.
**Exit:** F6 + F7 met on a real phone; device/browser coverage reported.

> **Open questions:** Q9 (which Avery label stock/sizes → default sheet templates),
> Q11 (confirm QR-only). Use documented defaults until answered.
