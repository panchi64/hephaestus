import { createSignal, For, Show, Suspense, type JSX } from "solid-js";
import {
  ASSET_IDS,
  Button,
  EmptyState,
  Field,
  Input,
  InstrumentBand,
  ListRow,
  LoadingText,
  Modal,
  PageHeader,
  Panel,
  Row,
  Select,
  Stack,
  StatusFlag,
  Text,
  confirm,
  toast,
} from "~/ui";
import { date, parseInstant } from "~/lib/format";
import { createInFlight } from "~/lib/inFlight";
import {
  useCalendars,
  useCalendarEvents,
  createEvent,
  updateEvent,
  deleteEvent,
  parseEventPhrase,
  syncCalendars,
  type EventWindow,
} from "../data";
import { CalendarMark } from "../components/CalendarMark";
import type { CalendarEvent, RecurrenceChoice } from "../model";

const DAYS_OF_WEEK = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

/** The repetitions the picker offers. `custom` is absent by construction — it is a rule
 *  the backend holds that this control can't express, so it can be shown but never chosen. */
const RECURRENCE_OPTIONS: { value: RecurrenceChoice; label: string }[] = [
  { value: "none", label: "No recurrence" },
  { value: "daily", label: "Daily" },
  { value: "weekly", label: "Weekly" },
  { value: "weekdays", label: "Weekdays" },
  { value: "monthly", label: "Monthly" },
];

/** Narrow the control's plain string back to a choice, via the list that produced it —
 *  so an unrecognised value falls back to "none" rather than being asserted into the type. */
function toChoice(value: string): RecurrenceChoice {
  return RECURRENCE_OPTIONS.find((o) => o.value === value)?.value ?? "none";
}

function getDaysInMonth(year: number, month: number): Date[] {
  const days: Date[] = [];
  // month is 0-indexed
  const firstDay = new Date(Date.UTC(year, month, 1));
  // Adjust to start on Monday (ISO week)
  let startDow = firstDay.getUTCDay(); // 0=Sun
  startDow = startDow === 0 ? 6 : startDow - 1; // 0=Mon

  // pad with previous month days
  for (let i = startDow - 1; i >= 0; i--) {
    days.push(new Date(Date.UTC(year, month, -i)));
  }
  // current month days
  const lastDay = new Date(Date.UTC(year, month + 1, 0)).getUTCDate();
  for (let d = 1; d <= lastDay; d++) {
    days.push(new Date(Date.UTC(year, month, d)));
  }
  // pad to complete final row (multiple of 7)
  while (days.length % 7 !== 0) {
    const last = days[days.length - 1];
    days.push(
      new Date(
        Date.UTC(
          last.getUTCFullYear(),
          last.getUTCMonth(),
          last.getUTCDate() + 1,
        ),
      ),
    );
  }
  return days;
}

/** An ISO instant as the wall-clock string a `datetime-local` input wants. Built from the
 *  local getters rather than by slicing the ISO string, which would show UTC and shift the
 *  event by the operator's offset the moment they saved it back. */
function toLocalInput(iso: string): string {
  // `parseInstant`, not `new Date` — a zone-less backend stamp read as local makes the
  // local getters below hand back the UTC digits, which is exactly the shift this
  // function exists to avoid.
  const d = new Date(parseInstant(iso));
  const pad = (n: number) => String(n).padStart(2, "0");
  return (
    `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}` +
    `T${pad(d.getHours())}:${pad(d.getMinutes())}`
  );
}

function sameDay(a: Date, b: Date): boolean {
  return (
    a.getUTCFullYear() === b.getUTCFullYear() &&
    a.getUTCMonth() === b.getUTCMonth() &&
    a.getUTCDate() === b.getUTCDate()
  );
}

const MONTH_NAMES = [
  "January",
  "February",
  "March",
  "April",
  "May",
  "June",
  "July",
  "August",
  "September",
  "October",
  "November",
  "December",
];

/** The span the grid needs loaded: the displayed month, padded a month either side so the
 *  grid's leading/trailing days and the UPCOMING panel have data without a second fetch. */
function monthWindow(year: number, month: number): EventWindow {
  return {
    start: new Date(Date.UTC(year, month - 1, 1)).toISOString(),
    end: new Date(Date.UTC(year, month + 2, 1)).toISOString(),
  };
}

export function CalendarScreen(): JSX.Element {
  const calendars = useCalendars();

  const today = new Date();
  const [viewYear, setViewYear] = createSignal(today.getUTCFullYear());
  const [viewMonth, setViewMonth] = createSignal(today.getUTCMonth());

  // The backend expands recurrences inside the window it's given, so navigating months
  // refetches rather than filtering a list the screen holds.
  const loaded = useCalendarEvents(() => monthWindow(viewYear(), viewMonth()));
  const events = () => loaded() ?? [];
  const [selectedEvent, setSelectedEvent] = createSignal<CalendarEvent | null>(
    null,
  );
  const [eventModalOpen, setEventModalOpen] = createSignal(false);
  const [newEventOpen, setNewEventOpen] = createSignal(false);
  const [quickAdd, setQuickAdd] = createSignal("");

  // Sync state
  const [syncing, setSyncing] = createSignal(false);

  // Both of these write events, and both are reachable twice before the first write
  // lands — Enter in the quick-add field, a second click on a slow Create. The claim is
  // keyed by what is being written (the phrase, the event being amended or "new") so a
  // repeat of the *same* request is what gets refused; there is one bar and one modal,
  // so `any()` is what the control reads.
  const quickAdding = createInFlight();
  const savingEvent = createInFlight();

  // One form for both create and edit — the two differ only in what seeds the fields and
  // where the save goes, so a second modal would be the same inputs drifting apart.
  // `editing` holds the event being amended, or null when composing a new one.
  const [editing, setEditing] = createSignal<CalendarEvent | null>(null);
  const [newTitle, setNewTitle] = createSignal("");
  const [newTitleError, setNewTitleError] = createSignal("");
  const [newStart, setNewStart] = createSignal("");
  const [newEnd, setNewEnd] = createSignal("");
  const [newLocation, setNewLocation] = createSignal("");
  const [newRecurrence, setNewRecurrence] =
    createSignal<RecurrenceChoice>("none");
  const [newCalendarId, setNewCalendarId] = createSignal("");

  const days = () => getDaysInMonth(viewYear(), viewMonth());

  const eventsForDay = (d: Date) =>
    events().filter((evt) => sameDay(new Date(parseInstant(evt.start)), d));

  const calendarForEvent = (evt: CalendarEvent) =>
    (calendars() ?? []).find((c) => c.id === evt.calendarId);

  // Derived once, read twice (label + tone). "All synced" over an empty list would be a
  // reassuring lie, so no calendars reads as exactly that.
  const syncStatus = (): {
    label: string;
    tone: "info" | "bright" | "warn";
  } => {
    if (syncing()) return { label: "Syncing…", tone: "info" };
    const rows = calendars() ?? [];
    if (rows.length === 0) return { label: "No calendars", tone: "warn" };
    return rows.every((c) => c.synced)
      ? { label: "All synced", tone: "bright" }
      : { label: "Partial", tone: "warn" };
  };

  const eventsThisMonth = () =>
    events().filter((evt) => {
      const d = new Date(parseInstant(evt.start));
      return (
        d.getUTCFullYear() === viewYear() && d.getUTCMonth() === viewMonth()
      );
    }).length;

  function prevMonth() {
    if (viewMonth() === 0) {
      setViewYear((y) => y - 1);
      setViewMonth(11);
    } else {
      setViewMonth((m) => m - 1);
    }
  }

  function nextMonth() {
    if (viewMonth() === 11) {
      setViewYear((y) => y + 1);
      setViewMonth(0);
    } else {
      setViewMonth((m) => m + 1);
    }
  }

  function goToday() {
    setViewYear(today.getUTCFullYear());
    setViewMonth(today.getUTCMonth());
  }

  function openEvent(evt: CalendarEvent) {
    setSelectedEvent(evt);
    setEventModalOpen(true);
  }

  // Reconcile every calendar bound to a remote server. The backend decides which those
  // are and reports the aggregate; this only phrases it.
  async function handleSync() {
    setSyncing(true);
    try {
      const { calendars: count, changed, failed } = await syncCalendars();
      if (count === 0) {
        toast.info("No remote calendars to sync");
      } else if (failed.length > 0) {
        toast.error(`SYNC FAILED FOR ${failed.join(", ").toUpperCase()}`);
      } else {
        toast.success(
          changed > 0
            ? `SYNC COMPLETE — ${changed} CHANGE${changed === 1 ? "" : "S"}`
            : "Sync complete — already up to date",
        );
      }
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Sync failed");
    } finally {
      setSyncing(false);
    }
  }

  // Delete the clicked entry. For a recurring event this cancels just that occurrence —
  // the backend keeps the rule — which is why the confirm text differs.
  async function handleDeleteEvent() {
    const evt = selectedEvent();
    if (!evt) return;
    const repeats = evt.recurring;
    const ok = await confirm({
      title: repeats
        ? `CANCEL THIS OCCURRENCE OF "${evt.title}"?`
        : `DELETE EVENT: "${evt.title}"?`,
      detail: repeats
        ? "The rest of the series is left in place. This cannot be undone."
        : "This cannot be undone.",
      confirmLabel: repeats ? "Cancel occurrence" : "Delete",
      cancelLabel: "Keep",
      tone: "alert",
    });
    if (!ok) return;
    try {
      await deleteEvent(evt);
      setEventModalOpen(false);
      setSelectedEvent(null);
      toast.success(repeats ? "Occurrence cancelled" : "Event deleted");
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Delete failed");
    }
  }

  function resetEventForm() {
    setEditing(null);
    setNewTitle("");
    setNewTitleError("");
    setNewStart("");
    setNewEnd("");
    setNewLocation("");
    setNewRecurrence("none");
    setNewCalendarId("");
  }

  function openCreateEvent() {
    resetEventForm();
    setNewEventOpen(true);
  }

  /** Amend the selected event. A rule the picker can't express (`custom` — an imported or
   *  CalDAV-synced series) seeds as `none`, so saving would silently flatten the series;
   *  the editor refuses instead of quietly rewriting what it can't represent. */
  function openEditEvent(evt: CalendarEvent) {
    if (evt.recurrence === "custom") {
      toast.error("This series' repeat rule can't be edited here");
      return;
    }
    setEditing(evt);
    setNewTitle(evt.title);
    setNewTitleError("");
    setNewStart(toLocalInput(evt.start));
    setNewEnd(evt.end ? toLocalInput(evt.end) : "");
    setNewLocation(evt.location ?? "");
    setNewRecurrence(toChoice(evt.recurrence ?? "none"));
    setNewCalendarId(evt.calendarId);
    setEventModalOpen(false);
    setNewEventOpen(true);
  }

  async function handleSaveEvent() {
    const title = newTitle().trim();
    if (!title) {
      setNewTitleError("Title is required");
      return;
    }
    const calId = resolvedCalendarId();
    if (!calId) {
      setNewTitleError("No calendar to add to");
      return;
    }
    setNewTitleError("");

    // A `datetime-local` value is wall-clock in the operator's own zone; `new Date(…)`
    // reads it that way, so the instant sent up is the one they picked.
    const startIso = newStart()
      ? new Date(newStart()).toISOString()
      : new Date().toISOString();
    const endIso = newEnd() ? new Date(newEnd()).toISOString() : undefined;
    const fields = {
      title,
      start: startIso,
      end: endIso,
      location: newLocation() || undefined,
      recurrence: newRecurrence(),
    };
    const target = editing();

    // The modal stays open until the write answers, which is the whole reason the guard
    // is here rather than on the button alone: for as long as it is open the Create
    // control is still under the pointer, and two presses on a slow link were two
    // identical events.
    await savingEvent.run(target?.id ?? "new", async () => {
      try {
        if (target) {
          await updateEvent(target, { ...fields, calendarId: calId });
        } else {
          await createEvent({ ...fields, calendarId: calId });
        }
      } catch (err) {
        toast.error(
          err instanceof Error
            ? err.message
            : target
              ? "Could not save event"
              : "Could not create event",
        );
        return;
      }

      setNewEventOpen(false);
      setSelectedEvent(null);
      toast.success(`Event "${title}" ${target ? "updated" : "created"}`);
      resetEventForm();
    });
  }

  // Quick add: the backend parses the phrase into a draft, which is created straight away.
  async function handleQuickAdd() {
    const phrase = quickAdd().trim();
    if (!phrase) {
      openCreateEvent();
      return;
    }
    const calId = resolvedCalendarId();
    if (!calId) {
      toast.error("No calendar to add to");
      return;
    }
    // Guarded here rather than on the Add button, because Enter in the field is a second
    // entry point into the same two writes — a phrase sent twice is one parse call and
    // one POST too many, and the duplicate lands on the operator's real calendar.
    await quickAdding.run(phrase, async () => {
      try {
        const draft = await parseEventPhrase(phrase);
        await createEvent({ ...draft, calendarId: calId });
        setQuickAdd("");
        toast.success(`Event "${draft.title}" created`);
      } catch (err) {
        toast.error(
          err instanceof Error ? err.message : "Could not read that phrase",
        );
      }
    });
  }

  // Initialise newCalendarId when calendars resolve.
  const resolvedCalendarId = () =>
    newCalendarId() || (calendars()?.[0]?.id ?? "");

  return (
    <Stack gap={6}>
      <PageHeader
        title="Calendar"
        subtitle="Events, scheduling, and CalDAV sync."
        assetId={ASSET_IDS.calendar}
        actions={
          <Row gap={2}>
            <Button
              variant="ghost"
              leading="refresh"
              size="sm"
              disabled={syncing()}
              onClick={handleSync}
            >
              {syncing() ? "Syncing…" : "Sync"}
            </Button>
            <Button variant="primary" leading="plus" onClick={openCreateEvent}>
              New event
            </Button>
          </Row>
        }
      />

      <Suspense fallback={<LoadingText label="Loading calendars" />}>
        <InstrumentBand
          items={[
            {
              label: "Month",
              value: `${MONTH_NAMES[viewMonth()]} ${viewYear()}`,
            },
            { label: "Events this month", value: String(eventsThisMonth()) },
            { label: "Calendars", value: String(calendars()?.length ?? 0) },
            {
              label: "Sync status",
              value: syncStatus().label,
              tone: syncStatus().tone,
            },
          ]}
        />
      </Suspense>

      <div class="flex min-h-0 gap-4">
        {/* Sidebar: calendars list */}
        <aside class="hidden w-48 shrink-0 flex-col gap-4 lg:flex">
          <Panel label="Calendars" flush>
            <Suspense
              fallback={
                <div class="p-3">
                  <LoadingText />
                </div>
              }
            >
              <Show
                when={(calendars()?.length ?? 0) > 0}
                fallback={
                  <div class="p-3">
                    <EmptyState
                      message="No calendars connected"
                      hint="Link or sync a calendar to get started."
                    />
                  </div>
                }
              >
                <For each={calendars()}>
                  {(cal) => (
                    <ListRow
                      label={
                        <Row gap={2} align="center">
                          <CalendarMark tone={cal.tone} />
                          <span>{cal.name}</span>
                        </Row>
                      }
                      right={
                        <StatusFlag>{cal.synced ? "Sync" : "Local"}</StatusFlag>
                      }
                    />
                  )}
                </For>
              </Show>
            </Suspense>
          </Panel>

          {/* Upcoming events */}
          <Panel label="Upcoming" flush>
            <Suspense
              fallback={
                <div class="p-3">
                  <LoadingText />
                </div>
              }
            >
              <For
                each={events()
                  .filter((e) => parseInstant(e.start) >= today.getTime())
                  .sort((a, b) => parseInstant(a.start) - parseInstant(b.start))
                  .slice(0, 5)}
                fallback={
                  <div class="p-3">
                    <EmptyState message="No upcoming events" />
                  </div>
                }
              >
                {(evt) => (
                  <ListRow
                    label={evt.title}
                    onClick={() => openEvent(evt)}
                    right={
                      <Text variant="micro" tone="dim">
                        {date(evt.start).slice(5)}
                      </Text>
                    }
                  />
                )}
              </For>
            </Suspense>
          </Panel>
        </aside>

        {/* Calendar main */}
        <div class="flex min-w-0 flex-1 flex-col gap-3">
          <Row justify="between" align="center">
            <Row gap={2} align="center">
              <Button
                variant="ghost"
                leading="chevron-left"
                size="sm"
                onClick={prevMonth}
              />
              <Text variant="readout" tone="bright">
                {MONTH_NAMES[viewMonth()]} {viewYear()}
              </Text>
              <Button
                variant="ghost"
                leading="chevron-right"
                size="sm"
                onClick={nextMonth}
              />
              <Button variant="default" size="sm" onClick={goToday}>
                Today
              </Button>
            </Row>
            {/* WEEK and DAY tabs lived here while the screen was mock-backed; they
                switched nothing. The month grid is the only view that exists, so it
                isn't offered as a choice until the other two do. */}
          </Row>

          {/* Quick-add bar */}
          <Row gap={2} align="center">
            <Input
              value={quickAdd()}
              onInput={(e) => setQuickAdd(e.currentTarget.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") void handleQuickAdd();
              }}
              placeholder="Quick add — e.g. 'Team sync Monday 14:00'"
              fieldClass="flex-1 min-w-0"
            />
            <Button
              variant="default"
              size="sm"
              leading="plus"
              pending={quickAdding.any()}
              onClick={handleQuickAdd}
            >
              {quickAdding.any() ? "Reading…" : "Add"}
            </Button>
          </Row>

          {/* Month grid */}
          <Suspense fallback={<LoadingText label="Loading events" />}>
            <Panel flush>
              {/* Day-of-week headers */}
              <div class="grid grid-cols-7 border-b border-line">
                <For each={DAYS_OF_WEEK}>
                  {(day) => (
                    <div class="border-r border-line px-2 py-1 last:border-r-0">
                      <Text variant="label" tone="dim">
                        {day}
                      </Text>
                    </div>
                  )}
                </For>
              </div>

              {/* Day cells */}
              <Show
                when={eventsThisMonth() > 0}
                fallback={
                  <div class="grid grid-cols-7">
                    <For each={days()}>
                      {(day) => {
                        const isCurrentMonth = () =>
                          day.getUTCMonth() === viewMonth();
                        const isToday = () => sameDay(day, today);
                        return (
                          <div
                            class="min-h-20 border-b border-r border-line p-1 last:border-r-0 transition-colors hover:bg-raised"
                            classList={{ "bg-raised": isToday() }}
                          >
                            <Text
                              variant="label"
                              tone={
                                isToday()
                                  ? "bright"
                                  : isCurrentMonth()
                                    ? "default"
                                    : "dim"
                              }
                              class="mb-1"
                            >
                              {isToday()
                                ? `[${day.getUTCDate()}]`
                                : String(day.getUTCDate())}
                            </Text>
                          </div>
                        );
                      }}
                    </For>
                    <div class="col-span-7 py-8">
                      <EmptyState
                        message="No events this month"
                        hint="Use NEW EVENT to schedule something."
                      />
                    </div>
                  </div>
                }
              >
                <div class="grid grid-cols-7">
                  <For each={days()}>
                    {(day) => {
                      const isCurrentMonth = () =>
                        day.getUTCMonth() === viewMonth();
                      const isToday = () => sameDay(day, today);
                      const dayEvents = () => eventsForDay(day);
                      return (
                        <div
                          class="min-h-20 border-b border-r border-line p-1 last:border-r-0 transition-colors hover:bg-raised"
                          classList={{
                            "bg-raised": isToday(),
                          }}
                        >
                          <Text
                            variant="label"
                            tone={
                              isToday()
                                ? "bright"
                                : isCurrentMonth()
                                  ? "default"
                                  : "dim"
                            }
                            class="mb-1"
                          >
                            {isToday()
                              ? `[${day.getUTCDate()}]`
                              : String(day.getUTCDate())}
                          </Text>
                          <Stack gap={0}>
                            <For each={dayEvents().slice(0, 3)}>
                              {(evt) => {
                                const cal = () => calendarForEvent(evt);
                                return (
                                  <button
                                    type="button"
                                    class="mb-0.5 flex w-full items-center gap-1 px-1 py-0.5 text-left text-label font-sans text-bright transition-colors hover:bg-raised"
                                    onClick={() => openEvent(evt)}
                                  >
                                    <Show when={cal()}>
                                      {(c) => <CalendarMark tone={c().tone} />}
                                    </Show>
                                    <span class="min-w-0 truncate">
                                      {evt.title}
                                    </span>
                                  </button>
                                );
                              }}
                            </For>
                            <Show when={dayEvents().length > 3}>
                              <Text variant="micro" tone="dim">
                                +{dayEvents().length - 3} MORE
                              </Text>
                            </Show>
                          </Stack>
                        </div>
                      );
                    }}
                  </For>
                </div>
              </Show>
            </Panel>
          </Suspense>
        </div>
      </div>

      {/* Event detail modal */}
      <Show when={selectedEvent()}>
        {(evt) => (
          <Modal
            open={eventModalOpen()}
            onClose={() => setEventModalOpen(false)}
            title="Event detail"
            footer={
              <Row gap={2}>
                <Button
                  variant="ghost"
                  onClick={() => setEventModalOpen(false)}
                >
                  Close
                </Button>
                <Button
                  variant="primary"
                  leading="edit"
                  onClick={() => openEditEvent(evt())}
                >
                  Edit
                </Button>
                <Button
                  variant="danger"
                  leading="trash"
                  onClick={handleDeleteEvent}
                >
                  Delete
                </Button>
              </Row>
            }
          >
            <Stack gap={4}>
              <Field label="Title" value={evt().title} />
              <Row gap={4}>
                <Field
                  label="Start"
                  value={evt().start.replace("T", " ").replace("Z", " UTC")}
                />
                <Field
                  label="End"
                  value={evt().end.replace("T", " ").replace("Z", " UTC")}
                />
              </Row>
              <Show when={evt().location}>
                <Field label="Location" value={evt().location!} />
              </Show>
              <Field
                label="Recurrence"
                value={(evt().recurrence ?? "none").toUpperCase()}
              />
              <Show when={evt().description}>
                <Field label="Description" value={evt().description!} />
              </Show>
              <Show when={calendarForEvent(evt())}>
                {(cal) => (
                  <Field
                    label="Calendar"
                    value={
                      <Row gap={2} align="center">
                        <CalendarMark tone={cal().tone} />
                        <StatusFlag>{cal().name.toUpperCase()}</StatusFlag>
                      </Row>
                    }
                  />
                )}
              </Show>
            </Stack>
          </Modal>
        )}
      </Show>

      {/* Event editor — one form for both new and existing (`CAL-1`) */}
      <Modal
        open={newEventOpen()}
        onClose={() => {
          setNewEventOpen(false);
          resetEventForm();
        }}
        title={editing() ? "Edit event" : "New event"}
        footer={
          <Row gap={2}>
            <Button
              variant="ghost"
              onClick={() => {
                setNewEventOpen(false);
                resetEventForm();
              }}
            >
              Cancel
            </Button>
            <Button
              variant="primary"
              leading={editing() ? "check" : "plus"}
              pending={savingEvent.any()}
              onClick={handleSaveEvent}
            >
              {editing() ? "Save" : "Create"}
            </Button>
          </Row>
        }
      >
        <Stack gap={4}>
          {/* The backend edits the series, not the instance — there is no per-occurrence
              override in the schema, so say so rather than let the operator assume. */}
          <Show when={editing()?.recurring}>
            <Text variant="micro" tone="warn">
              THIS EDITS THE WHOLE SERIES, NOT JUST THIS OCCURRENCE.
            </Text>
          </Show>
          <Input
            label="Title"
            value={newTitle()}
            onInput={(e) => {
              setNewTitle(e.currentTarget.value);
              if (newTitleError()) setNewTitleError("");
            }}
            placeholder="Event title"
            invalid={!!newTitleError()}
            hint={newTitleError() || undefined}
          />
          <Input
            label="Start"
            type="datetime-local"
            value={newStart()}
            onInput={(e) => setNewStart(e.currentTarget.value)}
          />
          <Input
            label="End"
            type="datetime-local"
            value={newEnd()}
            onInput={(e) => setNewEnd(e.currentTarget.value)}
          />
          <Input
            label="Location"
            value={newLocation()}
            onInput={(e) => setNewLocation(e.currentTarget.value)}
            placeholder="Optional location"
          />
          <Select
            label="Recurrence"
            value={newRecurrence()}
            onChange={(value) => setNewRecurrence(toChoice(value))}
            options={RECURRENCE_OPTIONS}
          />
          <Select
            label="Calendar"
            value={resolvedCalendarId()}
            onChange={setNewCalendarId}
            options={(calendars() ?? []).map((c) => ({
              value: c.id,
              label: c.name,
            }))}
          />
          <Text variant="micro" tone="dim">
            TIMEZONE:{" "}
            {Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC"}
          </Text>
        </Stack>
      </Modal>
    </Stack>
  );
}
