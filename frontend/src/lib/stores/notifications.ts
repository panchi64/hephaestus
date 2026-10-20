/**
 * Global notification store — the attention/notification surface's frontend
 * half. A separate surface from the chat/run stream: it never touches the
 * per-run event union, and it lives for the app's session rather than one
 * conversation.
 *
 * Backend-owned: read/unread policy, emit policy, and dedupe policy all live
 * server-side (`docs` notification design). This store only mirrors what the
 * backend reports — REST backfill on (re)connect, then live `notification.*`
 * SSE events merged on top — and relays intent (mark-read) back.
 *
 * Lifecycle: `startNotifications()` after the session is authenticated,
 * `stopNotifications()` on logout/lock (see `~/app/AppShell.tsx`, which hooks
 * the same `useSession()` signal the auth guard uses). A module-level
 * singleton, like `chatActivity`/`connectivity` — there is exactly one
 * operator, so exactly one notification feed.
 */
import { createSignal } from "solid-js";
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

/* --- Why there is no auto-clear here any more --------------------------------
 *
 * The bell used to carry an AUTO-CLEAR timeout (Off/5M/…/1H): a localStorage
 * preference that hid notifications older than the window *and*, on a 15s tick,
 * marked them read. It was wrong on three counts and every one of them was visible
 * to the operator.
 *
 * It **lied about what exists** — with 5M selected the panel read "No notifications"
 * over a backend holding five unread. It **destroyed the attention queue by the
 * clock**: picking a shorter window marked a swath of never-seen notifications read,
 * with no confirmation and no undo, and widening the window back brought the rows
 * back but not their unread state — so the same two settings disagreed depending on
 * which order they were touched. And it was a **policy in the wrong layer**: read
 * state is the backend's (this file's own header says so), and nothing here should be
 * mutating it on a timer.
 *
 * A notification is now read when the operator reads it — clicking a row, MARK ALL
 * READ, or the read-on-view policy when they open the conversation it is about. If
 * time-based dismissal is wanted again, it belongs in the backend as a stored policy
 * that fans out `notification.updated`, not in a display preference here.
 * --------------------------------------------------------------------------- */

// Backfill (REST) and the live stream connect concurrently so there's no gap
// between "as of the backfill query" and "first live event" — but that means
// stream events can arrive mid-flight, before we know the backfill's baseline
// unreadCount. Buffer them while a hydrate is in flight and replay them
// through the normal merge path once it lands, instead of racing two writers
// against `unreadCount`.
let hydrating = false;
let buffered: NotificationStreamEvent[] = [];

// Bumped on every start/stop so a hydrate() from a torn-down session can never
// write into a newer (or the stopped) session's state — belt-and-braces with
// the AbortSignal below: the signal usually cuts the fetch off outright, but a
// response that lands in the same tick as a stop/restart is still guarded by
// the generation check before any store write.
let generation = 0;

/** Upsert one notification by id — the same merge for both a fresh `created`
 *  and an `updated` (an update for an id already held just replaces it in
 *  place; a `created` that dupes an id already backfilled is a no-op replace,
 *  covering replay/backfill overlap). Adjusts `unreadCount` only on an actual
 *  read-state transition so re-applying the same notification twice can't
 *  double-count.
 *
 *  **An `updated` for an id we do not hold is an old notification, never a new one.**
 *  `items` holds the newest page, plus whatever `loadOlder` has paged in behind it, so a
 *  notification outside that window
 *  first reaches us when something changes it (an approval resolving, a read landing from
 *  another tab) — and the backfill's `unreadCount` already counted it. Counting it again
 *  on arrival inflates the badge past anything the operator can see or clear. It is still
 *  filed, in its own chronological place rather than at the head, since a list that
 *  claims to be newest-first must not open with an hour-old row. */
function upsert(
  notification: Notification,
  kind: "created" | "updated" = "created",
): void {
  const { items: next, previous: was } = upsertOrdered(items(), notification);
  setItems(next);
  if (!was) {
    if (kind === "created" && !notification.readAt) {
      setUnreadCount((c) => c + 1);
    }
    return;
  }
  if (!was.readAt && notification.readAt) {
    setUnreadCount((c) => Math.max(0, c - 1));
  } else if (was.readAt && !notification.readAt) {
    setUnreadCount((c) => c + 1);
  }
}

function applyDeleted(notification: Notification): void {
  const { items: next, removed } = removeRows(
    items(),
    new Set([notification.id]),
  );
  if (removed.length === 0) return;
  setItems(next);
  if (!removed[0].readAt) setUnreadCount((c) => Math.max(0, c - 1));
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

/** `gen` pins this call to the session that started it; `signal` ties the
 *  request to that session's AbortController so a stop() cuts it off outright.
 *  Both the success write and the finally's hydrating/buffered bookkeeping
 *  re-check `gen` against the current `generation` — a session that's since
 *  been torn down (or superseded by a new start) never touches live state. */
async function hydrate(gen: number, signal: AbortSignal): Promise<void> {
  hydrating = true;
  try {
    const page = await api.get<NotificationsPage>(
      `/notifications?limit=${PAGE_SIZE}`,
      { signal },
    );
    if (gen !== generation) return; // superseded — a newer session owns the store now
    setItems(page.items);
    setUnreadCount(page.unreadCount);
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

/** Start the live feed. Idempotent — safe to call from an effect that may
 *  re-fire while already running.
 *
 *  **The caller decides whether the session is authenticated; this does not.** It
 *  used to no-op without a bearer token, which is a *different* question — on a
 *  workspace with the auth gate disabled the session is legitimately unlocked and
 *  holds no token, so the entire notification surface silently never started: no
 *  backfill, no stream, a bell that read "No notifications" forever. `AppShell`
 *  starts this off `session.isAuthenticated`, which already accounts for the gate. */
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

/** Tear down the live feed and clear all state — logout/lock leaves nothing
 *  of the previous operator's notifications behind. Bumping `generation`
 *  invalidates any hydrate() still in flight (belt-and-braces alongside the
 *  abort — see `hydrate`'s doc comment) even if a response lands before its
 *  abort is observed. */
export function stopNotifications(): void {
  running = false;
  generation += 1;
  controller?.abort();
  controller = null;
  hydrating = false;
  buffered = [];
  setItems([]);
  setUnreadCount(0);
  setHasOlder(false);
  setLoadingOlder(false);
  setLastCreated(null);
  setConnectionState("idle");
}

/** Page one more window of history in, oldest-first from the oldest item held.
 *
 *  **The badge counts more than the list holds** — `unreadCount` is the backend's
 *  figure over every notification the operator owns, while the backfill is one page
 *  of the newest — so a red 9+ could sit over a panel with no unread row in it and
 *  no way to reach one. This is that way. It appends rather than replaces, and
 *  dedupes by id, because a live `created` may have landed on the head meanwhile.
 *
 *  `unreadCount` is deliberately *not* re-read from the page: an older page's count
 *  is the same global figure, and writing it back would clobber the optimistic
 *  decrement of a `markRead` still in flight. */
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
    setItems([...items(), ...page.items.filter((n) => !held.has(n.id))]);
    setHasOlder(page.items.length === PAGE_SIZE);
  } catch {
    /* best effort — the control stays offered, so a failed page is a retry away. */
  } finally {
    if (gen === generation) setLoadingOlder(false);
  }
}

/** Undo an optimistic read of exactly the rows this caller flipped, leaving every
 *  other row as it now stands.
 *
 *  **A rollback restores rows, not the array.** Both writers used to keep a snapshot
 *  of the whole list and put it back on failure, which silently reverted anything that
 *  had landed in the meantime: a live `notification.created`, or — now that the panel
 *  can page — the fifty older rows LOAD OLDER had just fetched. Pressing a row while
 *  the backend was briefly unreachable made them vanish, with `hasOlder` still true,
 *  so the only symptom was a list that got shorter. Undoing by id cannot reach
 *  anything the caller did not touch. */
function restoreUnread(flipped: readonly Notification[]): void {
  if (flipped.length === 0) return;
  setItems(restoreReadState(items(), flipped));
  setUnreadCount((c) => c + flipped.length);
}

/** Mark specific notifications read — optimistic, reconciled by the
 *  `notification.updated` events the backend fans out once it processes the
 *  request (which land through the normal `upsert` merge, so they're a no-op
 *  if the optimistic state already matches). Rolled back on failure. */
export async function markRead(ids: string[]): Promise<void> {
  if (ids.length === 0) return;
  // The rows this call actually flipped — its undo list, and its share of the badge.
  const { items: next, before: flipped } = setReadState(
    items(),
    new Set(ids),
    new Date().toISOString(),
  );
  setItems(next);
  if (flipped.length > 0)
    setUnreadCount((c) => Math.max(0, c - flipped.length));
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
  setItems(next);
  setUnreadCount((c) => c + flipped.length);
  try {
    await api.post("/notifications/unread", { ids });
  } catch {
    setItems(restoreReadState(items(), flipped));
    setUnreadCount((c) => Math.max(0, c - flipped.length));
  }
}

/** Remove notifications — optimistic; the removed rows are put back on failure. */
export async function dismiss(ids: string[]): Promise<void> {
  if (ids.length === 0) return;
  const { items: next, removed } = removeRows(items(), new Set(ids));
  setItems(next);
  const unreadRemoved = removed.filter((n) => !n.readAt).length;
  if (unreadRemoved > 0) setUnreadCount((c) => Math.max(0, c - unreadRemoved));
  try {
    await api.post("/notifications/delete", { ids });
  } catch {
    setItems(reinsertRows(items(), removed));
    setUnreadCount((c) => c + unreadRemoved);
  }
}

/** Remove every read notification — optimistic; the removed rows are put back on failure. */
export async function clearRead(): Promise<void> {
  const { items: next, removed } = removeRows(items(), (n) =>
    Boolean(n.readAt),
  );
  if (removed.length === 0) return;
  setItems(next);
  try {
    await api.post("/notifications/clear_read");
  } catch {
    setItems(reinsertRows(items(), removed));
  }
}

/** Mark every one of the operator's unread notifications read — the badge's own control.
 *
 *  **It relays and zeroes unconditionally, because the badge is not a count of this
 *  list.** `unreadCount` is the backend's figure over *every* notification the operator
 *  owns, while `items` holds only as far back as the operator has paged — so an unread
 *  one beyond that lights the badge without putting a single unread row in hand. Gating
 *  the whole call on finding an unread row locally made the button a no-op in exactly the
 *  state it is offered in: pressed, it marked nothing and the red count stayed up.
 *
 *  Rolling back restores the count that was on screen, not a count re-derived from the
 *  rows that happened to be loaded — the two are the same number only in the case that
 *  was never broken. The *rows* it puts back are only the ones it flipped, for the
 *  reason `restoreUnread` gives. */
export async function markAllRead(): Promise<void> {
  const prevUnread = unreadCount();
  const { items: next, before: flipped } = setReadState(
    items(),
    new Set(items().map((n) => n.id)),
    new Date().toISOString(),
  );
  setItems(next);
  setUnreadCount(0);
  try {
    await api.post("/notifications/read_all");
  } catch {
    // The count is restored wholesale rather than by `flipped.size`, since the badge
    // counts notifications this list never held.
    restoreUnread(flipped);
    setUnreadCount(prevUnread);
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
    /** Newest-first, exactly what the backend has handed over — the bell renders
     *  this list whole. Nothing here filters it; see the note above the store. */
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
