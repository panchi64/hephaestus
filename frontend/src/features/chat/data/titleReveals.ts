/**
 * Auto-title reveals.
 *
 * When the backend names a fresh thread it streams `conversation.titled` on the run, or,
 * when the name lands after the run ended, hands it to `collectLateTitle`. The
 * title is also persisted (the session list picks it up on the next refresh), but the
 * operator never asked for it — so the UI *types it out* rather than snapping it in. The
 * freshly-named title is held here, keyed by conversation id; the header and its sidebar
 * row reveal it with the typewriter.
 *
 * The reveal's lifetime is owned here, in the data layer — not by a mounted component's
 * animation-done callback. `revealTitle` schedules the clear up front, so an entry can
 * never leak if the operator navigates away mid-reveal, and either surface can render it
 * without one having to tell the other when it's done.
 */

import { createStore, produce } from "solid-js/store";

/** Milliseconds per character for a title reveal — shared by the typewriter and
 *  the clear-scheduling below so they stay in lockstep. */
export const REVEAL_SPEED_MS = 30;
// A buffer past the typed-out duration before clearing, so the clear always lands
// after the animation finishes — even for a sidebar row that mounts a beat late
// (a new thread's row appears on the post-turn refresh, just after the header
// began typing). When it clears, both surfaces fall back to the persisted title.
const REVEAL_CLEAR_BUFFER_MS = 1500;

const [titleReveals, setTitleReveals] = createStore<
  Record<string, string | undefined>
>({});
export { titleReveals };

export function revealTitle(id: string, title: string): void {
  setTitleReveals(id, title);
  const delay = title.length * REVEAL_SPEED_MS + REVEAL_CLEAR_BUFFER_MS;
  // Idempotent: clearing a since-deleted/renamed thread is a harmless no-op.
  setTimeout(() => setTitleReveals(produce((s) => void delete s[id])), delay);
}
