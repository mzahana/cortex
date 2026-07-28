import { useMemo, useState } from "react";
import {
  ActionIcon,
  Alert,
  Badge,
  Box,
  Button,
  Center,
  Group,
  Loader,
  Popover,
  ScrollArea,
  Stack,
  Text,
} from "@mantine/core";
import type { Asset, Me, Reservation } from "../../api/types";
import { dayKey, isSameDay, rangeFor, shiftReferenceDate } from "../reservations/dateRange";
import { STATUS_COLORS, formatWindow } from "../reservations/ReservationListItem";
import { useReservationList } from "../reservations/useReservationList";

const WEEKDAY_LABELS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
const BAR_HEIGHT = 20;
const BAR_GAP = 3;
// Below a phone's viewport width this many 7-column cells would be unusably
// narrow — below this, the grid scrolls horizontally instead of squishing
// (CLAUDE.md: every screen must stay usable one-handed on a phone, and the
// screen's own requirement explicitly allows "horizontal scroll ... is
// acceptable" for this exact tradeoff).
const MIN_GRID_WIDTH = 7 * 68;

function startOfDay(d: Date): Date {
  const out = new Date(d);
  out.setHours(0, 0, 0, 0);
  return out;
}

function addDays(d: Date, n: number): Date {
  const out = new Date(d);
  out.setDate(out.getDate() + n);
  return out;
}

function daysBetween(a: Date, b: Date): number {
  return Math.round((startOfDay(b).getTime() - startOfDay(a).getTime()) / 86_400_000);
}

/** `[startDay, endDayExclusive)` for a reservation, at day granularity — same
 * "end exactly at local midnight doesn't spill onto that day" rule
 * `ReservationsCalendarScreen`'s `itemsByDay` uses, so a reservation ending
 * at, say, 09:00 on the 5th still only occupies bars through the 4th/5th
 * consistently between the two screens. */
function reservationDaySpan(r: Reservation): { start: Date; endExclusive: Date } {
  const start = startOfDay(new Date(r.start_at));
  const end = new Date(r.end_at);
  const endDay = startOfDay(end);
  const endIsExactlyMidnight =
    end.getHours() === 0 && end.getMinutes() === 0 && end.getSeconds() === 0 && end.getMilliseconds() === 0;
  const endExclusive =
    endIsExactlyMidnight && endDay.getTime() > start.getTime() ? endDay : addDays(endDay, 1);
  return { start, endExclusive };
}

interface WeekSegment {
  reservation: Reservation;
  colStart: number; // 0-6, Monday-based
  colSpan: number; // 1-7
  continuesBefore: boolean;
  continuesAfter: boolean;
  lane: number;
}

/** Greedy same-row stacking (Google-Calendar-style): segments sorted by
 * start column (then longer-first), each placed in the first lane whose
 * previous occupant has already ended. Limitation: a reservation that spans
 * multiple week-rows can land in a different lane on each row (no
 * cross-row lane memory) — visually it can "jump" vertically at a week
 * boundary. Given real overlap for a single asset is rare (the F4 exclusion
 * constraint prevents two *active* — pending/approved/fulfilled —
 * reservations from overlapping; only historical rejected/cancelled/completed/
 * expired rows can stack against an active one or each other), this was judged an
 * acceptable simplification rather than a full interval-graph coloring. */
function assignLanes(segments: Omit<WeekSegment, "lane">[]): WeekSegment[] {
  const sorted = [...segments].sort((a, b) => a.colStart - b.colStart || b.colSpan - a.colSpan);
  const laneEndCol: number[] = [];
  const out: WeekSegment[] = [];
  for (const seg of sorted) {
    let lane = laneEndCol.findIndex((end) => end <= seg.colStart);
    if (lane === -1) {
      lane = laneEndCol.length;
      laneEndCol.push(0);
    }
    laneEndCol[lane] = seg.colStart + seg.colSpan;
    out.push({ ...seg, lane });
  }
  return out;
}

interface AssetReservationMonthCalendarProps {
  assetId: number;
  asset: Asset;
  me: Me;
}

/**
 * Google-Calendar-style month grid for one asset's reservations (Feature B
 * follow-up — the plain list in `AssetReservationsCard` doesn't show
 * *when in the month* a booking falls at a glance). Each reservation renders
 * as a colored bar spanning the days it covers, clipped and continued at
 * week-row boundaries; overlapping bars on the same day(s) stack into
 * separate lanes. Colors reuse `ReservationListItem`'s `STATUS_COLORS` so a
 * bar and its list-view badge always agree. Data comes from the same
 * `useReservationList` hook as every other reservation view, scoped to this
 * asset and the visible month's `[from, to)` window (CLAUDE.md: server-side
 * lists only, never "all reservations").
 */
export function AssetReservationMonthCalendar({ assetId, me }: AssetReservationMonthCalendarProps) {
  const [referenceDate, setReferenceDate] = useState(new Date());

  const { from, to } = useMemo(() => rangeFor("month", referenceDate), [referenceDate]);

  const filters = useMemo(
    () => ({
      asset: assetId,
      from: from.toISOString(),
      to: to.toISOString(),
      ordering: "start_at" as const,
    }),
    [assetId, from, to],
  );

  const { items, loading, error, reload } = useReservationList({ filters });

  const today = useMemo(() => new Date(), []);
  const weeks = useMemo(() => {
    const out: Date[][] = [];
    let cursor = new Date(from);
    while (cursor.getTime() < to.getTime()) {
      out.push(Array.from({ length: 7 }, (_, i) => addDays(cursor, i)));
      cursor = addDays(cursor, 7);
    }
    return out;
  }, [from, to]);

  const segmentsByWeek = useMemo(() => {
    return weeks.map((week) => {
      const rowStart = week[0];
      const rowEndExclusive = addDays(rowStart, 7);
      const raw: Omit<WeekSegment, "lane">[] = [];
      for (const r of items) {
        // Hide dead-end statuses per the "muted or hidden" requirement —
        // rejected/expired bookings never held the asset, so they'd just be
        // visual noise on a month grid meant for "when is this asset busy".
        // Cancelled and completed are kept but muted: cancelled *was* an
        // active hold until someone backed out, and completed *was* an
        // active hold that has since been checked in — in both cases the
        // window is free again for others to rebook, but the bar is still
        // informative context, not a current/active booking.
        if (r.status === "rejected" || r.status === "expired") continue;
        const { start, endExclusive } = reservationDaySpan(r);
        const segStart = start.getTime() > rowStart.getTime() ? start : rowStart;
        const segEndExclusive = endExclusive.getTime() < rowEndExclusive.getTime() ? endExclusive : rowEndExclusive;
        if (segStart.getTime() >= segEndExclusive.getTime()) continue;
        const colStart = daysBetween(rowStart, segStart);
        const colSpan = Math.max(1, daysBetween(segStart, segEndExclusive));
        raw.push({
          reservation: r,
          colStart,
          colSpan,
          continuesBefore: start.getTime() < rowStart.getTime(),
          continuesAfter: endExclusive.getTime() > rowEndExclusive.getTime(),
        });
      }
      return assignLanes(raw);
    });
  }, [weeks, items]);

  return (
    <Stack gap="xs">
      <Group justify="space-between">
        <ActionIcon
          variant="light"
          aria-label="Previous month"
          onClick={() => setReferenceDate((d) => shiftReferenceDate("month", d, -1))}
        >
          &#8249;
        </ActionIcon>
        <Text fw={600} size="sm">
          {referenceDate.toLocaleDateString(undefined, { month: "long", year: "numeric" })}
        </Text>
        <ActionIcon
          variant="light"
          aria-label="Next month"
          onClick={() => setReferenceDate((d) => shiftReferenceDate("month", d, 1))}
        >
          &#8250;
        </ActionIcon>
      </Group>
      <Button variant="subtle" size="xs" onClick={() => setReferenceDate(new Date())} style={{ alignSelf: "flex-start" }}>
        Today
      </Button>

      {error && (
        <Alert color="red" title="Couldn't load reservations">
          <Group justify="space-between">
            <Text size="sm">{error}</Text>
            <Button size="xs" variant="light" onClick={reload}>
              Retry
            </Button>
          </Group>
        </Alert>
      )}

      {loading && !error && (
        <Center p="md">
          <Loader size="sm" data-testid="asset-reservation-month-loading" />
        </Center>
      )}

      {!loading && !error && (
        <ScrollArea type="auto" offsetScrollbars>
          <Box style={{ minWidth: MIN_GRID_WIDTH }} data-testid="asset-reservation-month-grid">
            <Box
              style={{
                display: "grid",
                gridTemplateColumns: "repeat(7, 1fr)",
                borderBottom: "1px solid var(--mantine-color-default-border)",
                paddingBottom: 4,
              }}
            >
              {WEEKDAY_LABELS.map((label) => (
                <Text key={label} size="xs" c="dimmed" ta="center" fw={600}>
                  {label}
                </Text>
              ))}
            </Box>

            {weeks.map((week, wi) => {
              const segments = segmentsByWeek[wi];
              const laneCount = Math.max(1, ...segments.map((s) => s.lane + 1));
              return (
                <Box
                  key={dayKey(week[0])}
                  style={{ borderBottom: "1px solid var(--mantine-color-default-border)" }}
                >
                  <Box style={{ display: "grid", gridTemplateColumns: "repeat(7, 1fr)" }}>
                    {week.map((day) => {
                      const inMonth = day.getMonth() === referenceDate.getMonth();
                      return (
                        <Text
                          key={dayKey(day)}
                          size="xs"
                          ta="center"
                          pt={2}
                          c={inMonth ? undefined : "dimmed"}
                          fw={isSameDay(day, today) ? 700 : undefined}
                          style={
                            isSameDay(day, today)
                              ? {
                                  color: "var(--mantine-color-blue-6)",
                                }
                              : undefined
                          }
                        >
                          {day.getDate()}
                        </Text>
                      );
                    })}
                  </Box>
                  <Box
                    style={{
                      position: "relative",
                      minHeight: laneCount * (BAR_HEIGHT + BAR_GAP) + 4,
                      marginTop: 2,
                    }}
                  >
                    {segments.map((seg) => (
                      <ReservationBar key={`${seg.reservation.id}-${seg.colStart}`} segment={seg} me={me} />
                    ))}
                  </Box>
                </Box>
              );
            })}
          </Box>
        </ScrollArea>
      )}
    </Stack>
  );
}

function ReservationBar({ segment, me }: { segment: WeekSegment; me: Me }) {
  const { reservation, colStart, colSpan, continuesBefore, continuesAfter, lane } = segment;
  const color = STATUS_COLORS[reservation.status];
  const isOwn = reservation.user.id === me.id;
  const [opened, setOpened] = useState(false);

  return (
    <Popover opened={opened} onChange={setOpened} withArrow shadow="md" position="bottom" withinPortal>
      <Popover.Target>
        <Box
          role="button"
          tabIndex={0}
          data-testid={`reservation-bar-${reservation.id}`}
          onClick={() => setOpened((o) => !o)}
          onKeyDown={(e) => {
            if (e.key === "Enter" || e.key === " ") setOpened((o) => !o);
          }}
          style={{
            position: "absolute",
            left: `${(colStart / 7) * 100}%`,
            width: `${(colSpan / 7) * 100}%`,
            top: lane * (BAR_HEIGHT + BAR_GAP),
            height: BAR_HEIGHT,
            background: `var(--mantine-color-${color}-6)`,
            opacity: reservation.status === "cancelled" || reservation.status === "completed" ? 0.5 : 1,
            borderRadius: 4,
            borderTopLeftRadius: continuesBefore ? 0 : 4,
            borderBottomLeftRadius: continuesBefore ? 0 : 4,
            borderTopRightRadius: continuesAfter ? 0 : 4,
            borderBottomRightRadius: continuesAfter ? 0 : 4,
            paddingInline: 4,
            cursor: "pointer",
            display: "flex",
            alignItems: "center",
            overflow: "hidden",
          }}
        >
          <Text size="10px" c="white" truncate>
            {continuesBefore ? "← " : ""}
            {isOwn ? "You" : reservation.user.name || reservation.user.email}
            {continuesAfter ? " →" : ""}
          </Text>
        </Box>
      </Popover.Target>
      <Popover.Dropdown>
        <Stack gap={4} miw={200}>
          <Group justify="space-between">
            <Text size="sm" fw={600}>
              {isOwn ? "Your booking" : reservation.user.name || reservation.user.email}
            </Text>
            <Badge color={color} size="sm">
              {reservation.status}
            </Badge>
          </Group>
          <Text size="xs" c="dimmed">
            {formatWindow(reservation.start_at, reservation.end_at)}
          </Text>
          {reservation.approval_note && (
            <Text size="xs" c="dimmed">
              Note: {reservation.approval_note}
            </Text>
          )}
        </Stack>
      </Popover.Dropdown>
    </Popover>
  );
}
