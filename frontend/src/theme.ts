import { createTheme, type MantineColorsTuple } from "@mantine/core";

/**
 * Cortex design system (visual redesign — see `AppLayout`/nav for the
 * navigation shell this theme backs). Mobile-first defaults still apply:
 * comfortable tap targets, generous spacing, legible type at small sizes.
 *
 * `brand` is a tuned 10-shade indigo scale (a modern, professional
 * "product" blue-violet rather than Mantine's unmodified default blue) —
 * `manifest.webmanifest`'s `theme_color` is kept in sync with shade 6
 * (`#4f46e5`), the primary shade used in light mode.
 */
const brand: MantineColorsTuple = [
  "#eef1ff",
  "#e0e4fb",
  "#c1c7f5",
  "#9ea6ef",
  "#818aea",
  "#6c74e6",
  "#4f46e5",
  "#4338ca",
  "#372fb0",
  "#2b2590",
];

/** Secondary accent — a warm teal used sparingly for positive/"in stock"-
 * style highlights that need to read distinctly from `brand` on tiles/badges
 * (`DashboardTiles.tsx` and friends already lean on Mantine's built-in
 * red/orange/teal/grape for semantic status colors — this just gives the
 * palette a second first-class "product" hue for accents/illustrations). */
const accent: MantineColorsTuple = [
  "#e6fcf7",
  "#c3f7e9",
  "#96eed8",
  "#65e2c5",
  "#3ad3b3",
  "#1ebd9e",
  "#0ea083",
  "#0a8069",
  "#096854",
  "#075746",
];

export const theme = createTheme({
  colors: { brand, accent },
  primaryColor: "brand",
  primaryShade: { light: 6, dark: 5 },
  defaultRadius: "md",
  fontFamily:
    "-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif",
  headings: {
    fontFamily:
      "-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif",
    fontWeight: "700",
    sizes: {
      h1: { fontSize: "1.75rem", lineHeight: "1.3" },
      h2: { fontSize: "1.5rem", lineHeight: "1.35" },
      h3: { fontSize: "1.25rem", lineHeight: "1.4" },
      h4: { fontSize: "1.125rem", lineHeight: "1.4" },
    },
  },
  shadows: {
    xs: "0 1px 2px rgba(16, 24, 40, 0.06)",
    sm: "0 1px 3px rgba(16, 24, 40, 0.08), 0 1px 2px rgba(16, 24, 40, 0.06)",
    md: "0 4px 8px rgba(16, 24, 40, 0.08), 0 2px 4px rgba(16, 24, 40, 0.06)",
    lg: "0 12px 24px rgba(16, 24, 40, 0.10), 0 4px 8px rgba(16, 24, 40, 0.06)",
    xl: "0 20px 40px rgba(16, 24, 40, 0.14), 0 8px 16px rgba(16, 24, 40, 0.08)",
  },
  defaultGradient: { from: "brand.6", to: "accent.6", deg: 135 },
  components: {
    Card: {
      defaultProps: { radius: "lg" },
    },
    Paper: {
      defaultProps: { radius: "lg" },
    },
    Modal: {
      defaultProps: { radius: "lg" },
    },
    Button: {
      defaultProps: { radius: "md" },
    },
  },
});
