import type { ReactElement, ReactNode } from "react";
import { render, type RenderOptions } from "@testing-library/react";
import { MantineProvider } from "@mantine/core";
import { MemoryRouter } from "react-router-dom";
import { theme } from "../theme";

/**
 * Shared test wrapper: every screen/component under `src/screens` assumes a
 * `MantineProvider` ancestor (Mantine v7 throws without one — see
 * `@mantine/core`'s `useMantineTheme`) and several (anything routing-aware,
 * e.g. `AppLayout`) assume a router context. Mirrors `main.tsx`'s real
 * provider nesting so components see the same theme/context shape in tests
 * as in the app.
 */
function AllProviders({ children }: { children: ReactNode }) {
  return (
    <MantineProvider theme={theme} defaultColorScheme="light">
      <MemoryRouter>{children}</MemoryRouter>
    </MantineProvider>
  );
}

export function renderWithProviders(ui: ReactElement, options?: Omit<RenderOptions, "wrapper">) {
  return render(ui, { wrapper: AllProviders, ...options });
}

export * from "@testing-library/react";
