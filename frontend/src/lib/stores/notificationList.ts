/** Pure list operations behind the notification store. The list is newest-first by `createdAt`. */
import { parseInstant } from "~/lib/format";
import type { Notification } from "~/lib/stream/notificationEvents";

/** Insert in `createdAt` order, or replace in place when the id is already held. */
export function upsertOrdered(
  list: readonly Notification[],
  notification: Notification,
): { items: Notification[]; previous: Notification | undefined } {
  const idx = list.findIndex((n) => n.id === notification.id);
  const next = list.slice();
  if (idx !== -1) {
    next[idx] = notification;
    return { items: next, previous: list[idx] };
  }
  const at = list.findIndex(
    (n) => parseInstant(n.createdAt) <= parseInstant(notification.createdAt),
  );
  next.splice(at === -1 ? next.length : at, 0, notification);
  return { items: next, previous: undefined };
}

/** Drop the rows whose id is in `ids` (or, with a predicate, that match it); `removed` is what left. */
export function removeRows(
  list: readonly Notification[],
  drop: ReadonlySet<string> | ((n: Notification) => boolean),
): { items: Notification[]; removed: Notification[] } {
  const test =
    typeof drop === "function" ? drop : (n: Notification) => drop.has(n.id);
  const items: Notification[] = [];
  const removed: Notification[] = [];
  for (const n of list) (test(n) ? removed : items).push(n);
  return { items, removed };
}

/** Put `readAt` on every listed row whose read state differs; `before` holds those rows as they were. */
export function setReadState(
  list: readonly Notification[],
  ids: ReadonlySet<string>,
  readAt: string | null,
): { items: Notification[]; before: Notification[] } {
  const before: Notification[] = [];
  const items = list.map((n) => {
    if (!ids.has(n.id) || Boolean(n.readAt) === Boolean(readAt)) return n;
    before.push(n);
    return { ...n, readAt };
  });
  return { items, before };
}

/** Undo `setReadState` on exactly the rows it touched, leaving every other field and row as it now stands. */
export function restoreReadState(
  list: readonly Notification[],
  before: readonly Notification[],
): Notification[] {
  const prior = new Map(before.map((n) => [n.id, n.readAt]));
  return list.map((n) =>
    prior.has(n.id) ? { ...n, readAt: prior.get(n.id) ?? null } : n,
  );
}

/** Put removed rows back in order, skipping any id that has since been re-added. */
export function reinsertRows(
  list: readonly Notification[],
  rows: readonly Notification[],
): Notification[] {
  const held = new Set(list.map((n) => n.id));
  let items = list.slice();
  for (const row of rows) {
    if (!held.has(row.id)) items = upsertOrdered(items, row).items;
  }
  return items;
}
