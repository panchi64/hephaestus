/** Where a scroll region was left, remembered across mounts.
 *
 *  Five content renderers want this and none of them is the viewport — it followed the
 *  panel's state around only because that is where it was first needed. It is a generic
 *  bit of UI memory over `~/lib/storage`, capped so a long-lived install cannot grow an
 *  unbounded map of every file ever opened. */

import { onCleanup, onMount } from "solid-js";
import { readLS, writeLS } from "~/lib/storage";

const SCROLL_KEY = "heph.chat.viewer.scroll";
const SCROLL_LRU_CAP = 200;

function readScrollMap(): Record<string, number> {
  const raw = readLS(SCROLL_KEY);
  if (!raw) return {};
  try {
    return JSON.parse(raw) as Record<string, number>;
  } catch {
    return {};
  }
}

/** Insert/refresh `key` as the most-recently-used entry, evicting the oldest when
 *  over the cap. Plain-object key order is insertion order, so a delete+re-add
 *  moves `key` to the end and the first remaining key is genuinely the oldest. */
function touchScrollEntry(
  map: Record<string, number>,
  key: string,
  value: number,
): Record<string, number> {
  const next = { ...map };
  delete next[key];
  next[key] = value;
  const keys = Object.keys(next);
  if (keys.length > SCROLL_LRU_CAP) delete next[keys[0]];
  return next;
}

/** Restores `el.scrollTop` for `key()` after mount (next frame), then persists it
 *  debounced ~150ms on scroll, LRU-capped at 200 entries. Call from within a
 *  component's setup (uses `onMount`/`onCleanup` on the calling owner). */
export function rememberScroll(el: HTMLElement, key: () => string): void {
  let saveTimer: ReturnType<typeof setTimeout> | undefined;

  const onScroll = () => {
    if (saveTimer !== undefined) clearTimeout(saveTimer);
    const k = key();
    const y = el.scrollTop;
    saveTimer = setTimeout(() => {
      writeLS(
        SCROLL_KEY,
        JSON.stringify(touchScrollEntry(readScrollMap(), k, y)),
      );
    }, 150);
  };

  onMount(() => {
    requestAnimationFrame(() => {
      const y = readScrollMap()[key()];
      if (typeof y === "number") el.scrollTop = y;
    });
    el.addEventListener("scroll", onScroll, { passive: true });
  });

  onCleanup(() => {
    if (saveTimer !== undefined) clearTimeout(saveTimer);
    el.removeEventListener("scroll", onScroll);
  });
}
