import { createTheme } from "@mantine/core";

/** Mobile-first defaults: comfortable tap targets, primary blue matching the
 * manifest's `theme_color`. */
export const theme = createTheme({
  primaryColor: "blue",
  defaultRadius: "md",
  fontFamily:
    "-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif",
});
