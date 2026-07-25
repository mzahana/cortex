import { afterEach } from "vitest";
import { cleanup } from "@testing-library/react";
import "@testing-library/jest-dom/vitest";

// Explicit rather than relying on RTL's auto-cleanup-on-global-afterEach
// detection, since this project doesn't enable Vitest's Jest-style globals.
afterEach(() => {
  cleanup();
});

// jsdom doesn't implement `ResizeObserver` — Mantine's `ScrollArea` (used by
// `AppLayout`'s sidebar nav) observes size on mount.
if (!window.ResizeObserver) {
  window.ResizeObserver = class ResizeObserver {
    observe() {}
    unobserve() {}
    disconnect() {}
  };
}

// jsdom doesn't implement `window.matchMedia` — Mantine's `MantineProvider`
// (color scheme) and `useMediaQuery` (AppLayout's mobile/desktop nav) both
// call it unconditionally on mount, so every test that renders a Mantine
// tree needs this polyfill regardless of whether the test itself cares
// about media queries.
if (!window.matchMedia) {
  window.matchMedia = (query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addListener: () => {},
    removeListener: () => {},
    addEventListener: () => {},
    removeEventListener: () => {},
    dispatchEvent: () => false,
  }) as unknown as MediaQueryList;
}
