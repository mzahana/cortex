import { useMemo, useState } from "react";
import {
  ActionIcon,
  Alert,
  AppShell,
  Badge,
  Button,
  Center,
  Group,
  Loader,
  SegmentedControl,
  Stack,
  Text,
  Title,
} from "@mantine/core";
import { Calendar } from "@mantine/dates";
import { useNavigate } from "react-router-dom";
import { useAuth } from "../../hooks/useAuth";
import { hasAnyAssetPermission, RESERVATION_CREATE } from "../../api/permissions";
import type { Reservation } from "../../api/types";
import { useReservationList } from "./useReservationList";
import { dayKey, isSameDay, rangeFor, shiftReferenceDate, type CalendarViewMode } from "./dateRange";
import { ReservationListItem } from "./ReservationListItem";
import { CreateReservationModal } from "./CreateReservationModal";

/**
 * Reservations Calendar (T3.4, `docs/api-and-ui.md`: "Month/week/day; create/
 * approve; conflict feedback"). Mobile-first agenda: a month grid (dot =
 * has-reservations) for orientation, a week strip, and a day agenda for the
 * actual list/act-on-it view — the day agenda is what an approver actually
 * uses one-handed on a phone. Server-side `GET /reservations?from&to` drives
 * every view (never "all reservations").
 */
export function ReservationsCalendarScreen() {
  const navigate = useNavigate();
  const { me } = useAuth();

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
    <AppShell header={{ height: 60 }} padding="md">
      <AppShell.Header>
        <Group h="100%" px="md" justify="space-between">
          <Group gap="xs">
            <ActionIcon variant="subtle" aria-label="Back" onClick={() => navigate("/")}>
              &#8592;
            </ActionIcon>
            <Title order={4}>Reservations</Title>
          </Group>
          <Text size="sm" c="dimmed">
            {totalCount !== null ? `${items.length} of ${totalCount}` : ""}
          </Text>
        </Group>
      </AppShell.Header>

      <AppShell.Main>
        <Stack gap="sm">
          {banner && (
            <Alert color="teal" withCloseButton onClose={() => setBanner(null)}>
              {banner}
            </Alert>
          )}

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
        </Stack>
      </AppShell.Main>

      {canCreate && (
        <Button
          onClick={() => setCreateOpen(true)}
          data-testid="reserve-fab"
          radius="xl"
          size="lg"
          style={{ position: "fixed", right: 20, bottom: 20, boxShadow: "0 2px 8px rgba(0,0,0,0.3)" }}
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
    </AppShell>
  );
}
