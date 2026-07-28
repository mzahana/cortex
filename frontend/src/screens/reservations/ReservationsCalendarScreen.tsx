import { useMemo, useState } from "react";
import {
  ActionIcon,
  Alert,
  Badge,
  Button,
  Center,
  Group,
  Loader,
  SegmentedControl,
  Stack,
  Tabs,
  Text,
} from "@mantine/core";
import { Calendar } from "@mantine/dates";
import { useAuth } from "../../hooks/useAuth";
import { AppLayout } from "../../layout/AppLayout";
import { hasAnyAssetPermission, RESERVATION_CREATE } from "../../api/permissions";
import type { Reservation } from "../../api/types";
import { useReservationList } from "./useReservationList";
import { dayKey, isSameDay, rangeFor, shiftReferenceDate, type CalendarViewMode } from "./dateRange";
import { ReservationListItem } from "./ReservationListItem";
import { ReservationsListPanel } from "./ReservationsListPanel";
import { CreateReservationModal } from "./CreateReservationModal";

/**
 * Reservations Calendar + List (T3.4, `docs/api-and-ui.md`: "Month/week/day;
 * create/approve; conflict feedback"; List view added post-MVP, user-
 * requested: clicking through the calendar to find one specific reservation
 * to approve/cancel doesn't scale). A top-level `Tabs` picks between the two
 * — "Calendar" (this screen's original month/week/day agenda, unchanged) and
 * "List" (`ReservationsListPanel`: a filterable, paginated table/list of the
 * same underlying reservations, filters + row actions only, no new backend
 * endpoint). Kept as one screen/one route (`/reservations`) rather than a
 * second route so both views share the same nav entry and back-button
 * behavior — a user picking between "find it on a calendar" and "find it in
 * a list" is switching *view*, not *destination*.
 */
export function ReservationsCalendarScreen() {
  const { me } = useAuth();

  const [view, setView] = useState<"calendar" | "list">("calendar");
  const [mode, setMode] = useState<CalendarViewMode>("month");
  const [referenceDate, setReferenceDate] = useState<Date>(new Date());
  const [selectedDay, setSelectedDay] = useState<Date>(new Date());
  const [createOpen, setCreateOpen] = useState(false);
  const [banner, setBanner] = useState<string | null>(null);

  const { from, to } = useMemo(() => rangeFor(mode, referenceDate), [mode, referenceDate]);

  const filters = useMemo(
    () => ({ from: from.toISOString(), to: to.toISOString(), ordering: "start_at" as const }),
    [from, to],
  );

  const { items, assetsById, totalCount, loading, error, reload } = useReservationList({ filters });

  const itemsByDay = useMemo(() => {
    const map = new Map<string, Reservation[]>();
    for (const r of items) {
      // Reservations can span multiple days (bounded by
      // `RESERVATION_MAX_DURATION_DAYS` — a few dozen at most, never
      // unbounded), so this must show up on every local day it overlaps, not
      // just the day it starts on — otherwise a durable-asset booking still
      // in effect on day 2/3 would wrongly look "free" in the day agenda.
      const start = new Date(r.start_at);
      const end = new Date(r.end_at);
      const cursor = new Date(start.getFullYear(), start.getMonth(), start.getDate());
      const endDay = new Date(end.getFullYear(), end.getMonth(), end.getDate());
      // `[start_at, end_at)` — an end exactly at local midnight doesn't spill
      // onto that day.
      const endIsExactlyMidnight =
        end.getHours() === 0 && end.getMinutes() === 0 && end.getSeconds() === 0 && end.getMilliseconds() === 0;
      if (endIsExactlyMidnight && endDay.getTime() > cursor.getTime()) {
        endDay.setDate(endDay.getDate() - 1);
      }
      while (cursor.getTime() <= endDay.getTime()) {
        const key = dayKey(cursor);
        const list = map.get(key) ?? [];
        list.push(r);
        map.set(key, list);
        cursor.setDate(cursor.getDate() + 1);
      }
    }
    return map;
  }, [items]);

  const selectedDayItems = itemsByDay.get(dayKey(selectedDay)) ?? [];

  const handleReservationChanged = (updated: Reservation) => {
    setBanner(`Reservation ${updated.status}.`);
    reload();
  };

  const handleCreated = (created: Reservation) => {
    setBanner(`Reservation ${created.status === "pending" ? "requested (pending approval)" : "confirmed"}.`);
    setSelectedDay(new Date(created.start_at));
    reload();
  };

  if (!me) {
    return (
      <Center h="100vh">
        <Loader />
      </Center>
    );
  }

  const canCreate = hasAnyAssetPermission(me, RESERVATION_CREATE);

  return (
    <AppLayout
      title="Reservations"
      actions={
        view === "calendar" ? (
          <Text size="sm" c="dimmed">
            {totalCount !== null ? `${items.length} of ${totalCount}` : ""}
          </Text>
        ) : null
      }
    >
        <Stack gap="sm" pb={72}>
          {banner && (
            <Alert color="teal" withCloseButton onClose={() => setBanner(null)}>
              {banner}
            </Alert>
          )}

          <Tabs value={view} onChange={(v) => setView((v as "calendar" | "list") ?? "calendar")}>
            <Tabs.List grow>
              <Tabs.Tab value="calendar" data-testid="reservations-tab-calendar">
                Calendar
              </Tabs.Tab>
              <Tabs.Tab value="list" data-testid="reservations-tab-list">
                List
              </Tabs.Tab>
            </Tabs.List>
          </Tabs>

          {view === "list" && <ReservationsListPanel me={me} />}

          {view === "calendar" && (
            <>
          <SegmentedControl
            fullWidth
            value={mode}
            onChange={(v) => setMode(v as CalendarViewMode)}
            data={[
              { label: "Month", value: "month" },
              { label: "Week", value: "week" },
              { label: "Day", value: "day" },
            ]}
          />

          <Group justify="space-between">
            <ActionIcon
              variant="light"
              aria-label="Previous"
              onClick={() => setReferenceDate((d) => shiftReferenceDate(mode, d, -1))}
            >
              &#8249;
            </ActionIcon>
            <Text fw={600}>
              {mode === "month"
                ? referenceDate.toLocaleDateString(undefined, { month: "long", year: "numeric" })
                : mode === "week"
                  ? `Week of ${from.toLocaleDateString(undefined, { month: "short", day: "numeric" })}`
                  : referenceDate.toLocaleDateString(undefined, {
                      weekday: "long",
                      month: "short",
                      day: "numeric",
                    })}
            </Text>
            <ActionIcon
              variant="light"
              aria-label="Next"
              onClick={() => setReferenceDate((d) => shiftReferenceDate(mode, d, 1))}
            >
              &#8250;
            </ActionIcon>
          </Group>
          <Button
            variant="subtle"
            size="xs"
            onClick={() => {
              const now = new Date();
              setReferenceDate(now);
              setSelectedDay(now);
            }}
          >
            Today
          </Button>

          {error && (
            <Alert color="red" title="Couldn't load reservations">
              <Stack gap="xs" align="flex-start">
                <Text size="sm">{error}</Text>
                <Button size="xs" variant="light" onClick={reload}>
                  Retry
                </Button>
              </Stack>
            </Alert>
          )}

          {loading && !error && (
            <Center p="xl">
              <Loader data-testid="reservations-loading" />
            </Center>
          )}

          {!loading && !error && mode === "month" && (
            <Calendar
              date={referenceDate}
              onDateChange={setReferenceDate}
              renderDay={(date) => {
                const count = (itemsByDay.get(dayKey(date)) ?? []).length;
                return (
                  <div style={{ position: "relative" }}>
                    <div>{date.getDate()}</div>
                    {count > 0 && (
                      <div
                        style={{
                          position: "absolute",
                          bottom: -2,
                          left: "50%",
                          transform: "translateX(-50%)",
                          width: 4,
                          height: 4,
                          borderRadius: 4,
                          background: "var(--mantine-color-blue-6)",
                        }}
                      />
                    )}
                  </div>
                );
              }}
              getDayProps={(date) => ({
                selected: isSameDay(date, selectedDay),
                onClick: () => {
                  setSelectedDay(date);
                  setMode("day");
                  setReferenceDate(date);
                },
              })}
            />
          )}

          {!loading && !error && mode === "week" && (
            <Stack gap="xs">
              {Array.from({ length: 7 }, (_, i) => {
                const d = new Date(from);
                d.setDate(d.getDate() + i);
                const dayItems = itemsByDay.get(dayKey(d)) ?? [];
                return (
                  <Group
                    key={dayKey(d)}
                    justify="space-between"
                    p="xs"
                    style={{ cursor: "pointer", borderRadius: 8 }}
                    bg={isSameDay(d, selectedDay) ? "var(--mantine-color-blue-light)" : undefined}
                    onClick={() => {
                      setSelectedDay(d);
                      setMode("day");
                      setReferenceDate(d);
                    }}
                  >
                    <Text size="sm">
                      {d.toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" })}
                    </Text>
                    {dayItems.length > 0 ? (
                      <Badge size="sm" variant="light">
                        {dayItems.length}
                      </Badge>
                    ) : (
                      <Text size="xs" c="dimmed">
                        —
                      </Text>
                    )}
                  </Group>
                );
              })}
            </Stack>
          )}

          {!loading && !error && mode === "day" && (
            <Stack gap="xs">
              {selectedDayItems.length === 0 && (
                <Center p="lg">
                  <Text c="dimmed" size="sm">
                    No reservations this day.
                  </Text>
                </Center>
              )}
              {selectedDayItems.map((r) => (
                <ReservationListItem
                  key={r.id}
                  reservation={r}
                  asset={assetsById.get(r.asset)}
                  me={me}
                  onChanged={handleReservationChanged}
                />
              ))}
            </Stack>
          )}

          {totalCount !== null && totalCount > items.length && (
            <Text size="xs" c="dimmed" ta="center">
              Showing {items.length} of {totalCount} in this range — narrow the range to see the rest.
            </Text>
          )}
            </>
          )}
        </Stack>

      {canCreate && (
        <Button
          onClick={() => setCreateOpen(true)}
          data-testid="reserve-fab"
          radius="xl"
          size="lg"
          style={{
            position: "fixed",
            right: 20,
            // Clears the mobile bottom tab bar (~76px); on desktop there's
            // no footer so the extra offset just reads as normal FAB margin.
            bottom: "calc(20px + var(--app-bottom-nav-offset, 0px))",
            boxShadow: "var(--mantine-shadow-lg)",
            zIndex: 190,
          }}
        >
          + Reserve
        </Button>
      )}

      <CreateReservationModal
        opened={createOpen}
        onClose={() => setCreateOpen(false)}
        onCreated={handleCreated}
        initialStart={selectedDay}
      />
    </AppLayout>
  );
}
