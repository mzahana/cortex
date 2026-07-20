/** Month/week/day range math for the Reservations Calendar (T3.4). Kept as
 * plain `Date` arithmetic (no dayjs dependency here) — `@mantine/dates`
 * already pulls in dayjs for its own components. */

export type CalendarViewMode = "month" | "week" | "day";

function startOfDay(d: Date): Date {
  const out = new Date(d);
  out.setHours(0, 0, 0, 0);
  return out;
}

/** Monday-based week start (matches `@mantine/dates`' default `firstDayOfWeek`). */
function startOfWeek(d: Date): Date {
  const out = startOfDay(d);
  const day = out.getDay(); // 0 = Sunday
  const diff = (day + 6) % 7; // days since Monday
  out.setDate(out.getDate() - diff);
  return out;
}

function startOfMonth(d: Date): Date {
  return new Date(d.getFullYear(), d.getMonth(), 1);
}

/** `[from, to)` window for the given view mode + reference date, as ISO-8601
 * datetime strings (`GET /reservations?from&to` per `docs/api-and-ui.md`). */
export function rangeFor(mode: CalendarViewMode, referenceDate: Date): { from: Date; to: Date } {
  if (mode === "day") {
    const from = startOfDay(referenceDate);
    const to = new Date(from);
    to.setDate(to.getDate() + 1);
    return { from, to };
  }
  if (mode === "week") {
    const from = startOfWeek(referenceDate);
    const to = new Date(from);
    to.setDate(to.getDate() + 7);
    return { from, to };
  }
  // month — full 6-week grid so the rendered calendar's leading/trailing
  // outside-month days are covered too.
  const monthStart = startOfMonth(referenceDate);
  const from = startOfWeek(monthStart);
  const nextMonthStart = new Date(monthStart.getFullYear(), monthStart.getMonth() + 1, 1);
  const to = startOfWeek(nextMonthStart);
  to.setDate(to.getDate() + 7);
  return { from, to };
}

export function shiftReferenceDate(mode: CalendarViewMode, referenceDate: Date, dir: 1 | -1): Date {
  const out = new Date(referenceDate);
  if (mode === "day") out.setDate(out.getDate() + dir);
  else if (mode === "week") out.setDate(out.getDate() + 7 * dir);
  else out.setMonth(out.getMonth() + dir);
  return out;
}

export function isSameDay(a: Date, b: Date): boolean {
  return (
    a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() && a.getDate() === b.getDate()
  );
}

/** `yyyy-mm-dd` local-date key (grouping helper — never used for wire format,
 * `Reservation.start_at`/`end_at` are always the server's own ISO strings). */
export function dayKey(d: Date): string {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}
