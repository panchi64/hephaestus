import { createEffect, onCleanup } from "solid-js";
import { parseInstant } from "~/lib/format";
import { refreshPrivateStatus } from "./actions";
import { markLocked, privateStatus } from "./store";

/** Shut the area on the client when the backend's relock time passes, and re-read it
 *  when the window regains focus, since any use elsewhere moves that time. Asks nothing
 *  while `active` is false (signed out, where every request would bounce). */
export function useRelockTimer(active: () => boolean): void {
  createEffect(() => {
    if (active()) void refreshPrivateStatus().catch(() => undefined);
    else markLocked();
  });

  createEffect(() => {
    const at = privateStatus()?.relock_at;
    if (!at) return;
    const wait = parseInstant(at) - Date.now();
    if (Number.isNaN(wait)) return;
    // A second late, so the backend has already shut it when the re-read lands. The
    // re-read decides, since a use elsewhere may have held it open; with no answer the
    // client shuts it anyway.
    const timer = setTimeout(
      () => {
        void refreshPrivateStatus().catch(markLocked);
      },
      Math.max(0, wait) + 1000,
    );
    onCleanup(() => clearTimeout(timer));
  });

  const onFocus = (): void => {
    if (active()) void refreshPrivateStatus().catch(() => undefined);
  };
  window.addEventListener("focus", onFocus);
  onCleanup(() => window.removeEventListener("focus", onFocus));
}
