/**
 * Global notification store, separate from the per-run chat stream and alive for the
 * app's session.
 *
 * Read, emit and dedupe policy are the backend's. This store mirrors it — REST backfill
 * on (re)connect, live `notification.*` SSE events merged on top — and relays intent
 * (mark-read) back. A notification is read when the operator reads it: clicking a row,
 * MARK ALL READ, or opening the conversation it is about. Nothing here changes read state
 * on a timer.
 *
 * `startNotifications()` once the session is authenticated, `stopNotifications()` on
 * logout/lock (`~/app/AppShell.tsx`). A module-level singleton: one operator, one feed.
 */
import { batch, createSignal } from "solid-js";
import { api } from "~/lib/api";
import {
  reinsertRows,
  removeRows,
  restoreReadState,
  setReadState,
  upsertOrdered,
} from "./notificationList";
import {
  streamNotifications,
  type NotificationStreamState,
} from "~/lib/stream/notificationStream";
import type {
  Notification,
  NotificationsPage,
  NotificationStreamEvent,
} from "~/lib/stream/notificationEvents";

/** How many notifications to backfill on connect, and how many each LOAD OLDER
 *  adds. The panel pages further back through the endpoint's own `before`
 *  cursor — see `loadOlder`. */
const PAGE_SIZE = 50;

const [items, setItems] = createSignal<Notification[]>([]);
const [unreadCount, setUnreadCount] = createSignal(0);
const [connectionState, setConnectionState] = createSignal<
  NotificationStreamState | "idle"
>("idle");
// Whether the backend has notifications older than the oldest one held. A full page
// means "probably more"; the first short page is the end of the history.
const [hasOlder, setHasOlder] = createSignal(false);
const [loadingOlder, setLoadingOlder] = createSignal(false);
// The newest notification the live stream created. Never set by a backfill, so a
// reconnect does not replay history as arrivals.
const [lastCreated, setLastCreated] = createSignal<Notification | null>(null);

let controller: AbortController | null = null;
let running = false;

// Backfill and stream connect concurrently, so stream events can land before the
// backfill's baseline `unreadCount`. They are buffered while a hydrate is in flight and
// replayed through the normal merge once it lands.
let hydrating = false;
let buffered: NotificationStreamEvent[] = [];

// Bumped on every start/stop so a hydrate() from a torn-down session never writes into a
// newer one's state, even when its response lands in the same tick as the abort.
let generation = 0;

/** Write the list and the badge as one change. Read-on-view reacts to the list by marking
 *  the open thread's rows read; run between two separate writes, its decrement would be
 *  overwritten and leave an unread count with no unread row. */
function commit(next: Notification[], count?: (c: number) => number): void {
  batch(() => {
    setItems(next);
    if (count) setUnreadCount(count);
  });
}

/** Merge one notification by id, for `created` and `updated` alike. `unreadCount` moves
 *  only on a real read-state transition, so replaying an event can't double-count.
 *
 *  An `updated` for an id not held is an old notification outside the loaded pages, which
 *  the backfill's `unreadCount` already counted: it is filed in chronological place and
 *  not counted again. */
function upsert(
  notification: Notification,
  kind: "created" | "updated" = "created",
): void {
  const { items: next, previous: was } = upsertOrdered(items(), notification);
  const arrivedUnread = !was && kind === "created" && !notification.readAt;
  const becameUnread = was?.readAt && !notification.readAt;
  const becameRead = was && !was.readAt && notification.readAt;
  if (arrivedUnread || becameUnread) commit(next, (c) => c + 1);
  else if (becameRead) commit(next, (c) => Math.max(0, c - 1));
  else commit(next);
}

function applyDeleted(notification: Notification): void {
  const { items: next, removed } = removeRows(
    items(),
    new Set([notification.id]),
  );
  if (removed.length === 0) return;
  commit(next, removed[0].readAt ? undefined : (c) => Math.max(0, c - 1));
}

function applyStreamEvent(event: NotificationStreamEvent): void {
  if (event.type === "notification.deleted") {
    applyDeleted(event.notification);
    return;
  }
  const created = event.type === "notification.created";
  upsert(event.notification, created ? "created" : "updated");
  if (created) setLastCreated(event.notification);
}

function handleStreamEvent(event: NotificationStreamEvent): void {
  if (hydrating) {
    buffered.push(event);
    return;
  }
  applyStreamEvent(event);
}

/** `gen` and `signal` pin this call to the session that started it; every write
 *  re-checks `gen`, so a torn-down or superseded session never touches live state. */
async function hydrate(gen: number, signal: AbortSignal): Promise<void> {
  hydrating = true;
  try {
    const page = await api.get<NotificationsPage>(
      `/notifications?limit=${PAGE_SIZE}`,
      { signal },
    );
    if (gen !== generation) return; // superseded — a newer session owns the store now
    commit(page.items, () => page.unreadCount);
    setHasOlder(page.items.length === PAGE_SIZE);
  } catch {
    /* best effort — the live stream still delivers new notifications; the
     * next reconnect (or an explicit re-hydrate) retries the backfill. */
  } finally {
    if (gen !== generation) return; // don't clobber a newer session's hydrating/buffered
    hydrating = false;
    const pending = buffered;
    buffered = [];
    for (const event of pending) applyStreamEvent(event);
  }
}

/** Start the live feed. Idempotent.
 *
 *  The caller decides whether the session is authenticated; holding no token is not the
 *  same fact, since a workspace with the auth gate off is unlocked without one. */
export function startNotifications(): void {
  if (running) return;
  running = true;
  generation += 1;
  const gen = generation;
  const ac = new AbortController();
  controller = ac;
  setConnectionState("connecting");
  void streamNotifications({
    signal: ac.signal,
    onEvent: handleStreamEvent,
    onStateChange: setConnectionState,
  });
  void hydrate(gen, ac.signal);
}

/** Tear down the live feed and clear all state, so logout/lock leaves nothing behind. */
export function stopNotifications(): void {
  running = false;
  generation += 1;
  controller?.abort();
  controller = null;
  hydrating = false;
  buffered = [];
  commit([], () => 0);
  setHasOlder(false);
  setLoadingOlder(false);
  setLastCreated(null);
  setConnectionState("idle");
}

/** Page one more window of history in behind the oldest item held — the way to reach
 *  unread rows the badge counts but the first page doesn't hold. Dedupes by id, since a
 *  live `created` may have landed meanwhile.
 *
 *  `unreadCount` is not re-read from the page: it is the same global figure, and writing
 *  it back would clobber the optimistic decrement of a `markRead` in flight. */
export async function loadOlder(): Promise<void> {
  if (loadingOlder() || !hasOlder()) return;
  const oldest = items()[items().length - 1];
  if (!oldest) return;
  const gen = generation;
  setLoadingOlder(true);
  try {
    const page = await api.get<NotificationsPage>(
      `/notifications?limit=${PAGE_SIZE}&before=${encodeURIComponent(oldest.createdAt)}`,
      { signal: controller?.signal },
    );
    if (gen !== generation) return; // superseded — a newer session owns the store now
    const held = new Set(items().map((n) => n.id));
    commit([...items(), ...page.items.filter((n) => !held.has(n.id))]);
    setHasOlder(page.items.length === PAGE_SIZE);
  } catch {
    /* best effort — the control stays offered, so a failed page is a retry away. */
  } finally {
    if (gen === generation) setLoadingOlder(false);
  }
}

/** Undo an optimistic read of exactly the rows this caller flipped. A rollback restores
 *  rows by id, never a snapshot of the list, so rows that landed meanwhile (a live
 *  `created`, a page LOAD OLDER fetched) survive it. */
function restoreUnread(flipped: readonly Notification[]): void {
  if (flipped.length === 0) return;
  commit(restoreReadState(items(), flipped), (c) => c + flipped.length);
}

/** Mark specific notifications read — optimistic, rolled back on failure. The backend's
 *  `notification.updated` echoes merge as no-ops. */
export async function markRead(ids: string[]): Promise<void> {
  if (ids.length === 0) return;
  // The rows this call actually flipped — its undo list, and its share of the badge.
  const { items: next, before: flipped } = setReadState(
    items(),
    new Set(ids),
    new Date().toISOString(),
  );
  commit(next, (c) => Math.max(0, c - flipped.length));
  try {
    await api.post("/notifications/read", { ids });
  } catch {
    restoreUnread(flipped);
  }
}

/** Mark specific notifications unread — optimistic, rolled back on failure. */
export async function markUnread(ids: string[]): Promise<void> {
  if (ids.length === 0) return;
  const { items: next, before: flipped } = setReadState(
    items(),
    new Set(ids),
    null,
  );
  commit(next, (c) => c + flipped.length);
  try {
    await api.post("/notifications/unread", { ids });
  } catch {
    commit(restoreReadState(items(), flipped), (c) =>
      Math.max(0, c - flipped.length),
    );
  }
}

/** Remove notifications — optimistic; the removed rows are put back on failure. */
export async function dismiss(ids: string[]): Promise<void> {
  if (ids.length === 0) return;
  const { items: next, removed } = removeRows(items(), new Set(ids));
  const unreadRemoved = removed.filter((n) => !n.readAt).length;
  commit(next, (c) => Math.max(0, c - unreadRemoved));
  try {
    await api.post("/notifications/delete", { ids });
  } catch {
    commit(reinsertRows(items(), removed), (c) => c + unreadRemoved);
  }
}

/** Remove every read notification — optimistic; the removed rows are put back on failure. */
export async function clearRead(): Promise<void> {
  const { items: next, removed } = removeRows(items(), (n) =>
    Boolean(n.readAt),
  );
  if (removed.length === 0) return;
  commit(next);
  try {
    await api.post("/notifications/clear_read");
  } catch {
    commit(reinsertRows(items(), removed));
  }
}

/** Mark every one of the operator's unread notifications read.
 *
 *  It relays and zeroes unconditionally: the badge counts every notification the operator
 *  owns, including ones beyond the pages loaded here. A rollback restores the count that
 *  was on screen, and only the rows this call flipped. */
export async function markAllRead(): Promise<void> {
  const prevUnread = unreadCount();
  const { items: next, before: flipped } = setReadState(
    items(),
    new Set(items().map((n) => n.id)),
    new Date().toISOString(),
  );
  commit(next, () => 0);
  try {
    await api.post("/notifications/read_all");
  } catch {
    commit(restoreReadState(items(), flipped), () => prevUnread);
  }
}

/** Mark a conversation's unread notifications read, skipping `except`, and return the
 *  ids it marked. The read-on-view policy calls this from the chat screen and passes
 *  what it already marked, so a row the operator set back to unread stays unread. */
export function markConversationRead(
  conversationId: string,
  except: ReadonlySet<string> = new Set(),
): string[] {
  const ids = items()
    .filter(
      (n) =>
        n.conversationId === conversationId && !n.readAt && !except.has(n.id),
    )
    .map((n) => n.id);
  void markRead(ids);
  return ids;
}

export function useNotifications() {
  return {
    /** Newest-first, unfiltered: exactly what the backend has handed over. */
    get items(): Notification[] {
      return items();
    },
    get unreadCount(): number {
      return unreadCount();
    },
    get connectionState(): NotificationStreamState | "idle" {
      return connectionState();
    },
    /** Whether there is history beyond what is held — the panel's LOAD OLDER. */
    get hasOlder(): boolean {
      return hasOlder();
    },
    get loadingOlder(): boolean {
      return loadingOlder();
    },
    get lastCreated(): Notification | null {
      return lastCreated();
    },
    loadOlder,
    markRead,
    markUnread,
    dismiss,
    clearRead,
    markAllRead,
    markConversationRead,
  };
}
