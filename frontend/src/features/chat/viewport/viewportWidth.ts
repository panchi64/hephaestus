/**
 * How wide the panel is, and how much room the surfaces inside it actually have.
 *
 * Width used to live with the persisted preferences because there was one surface and one
 * number. With a set of them it becomes layout arithmetic — a floor derived from what is
 * open, a measured box the splitting policy consults — and none of that is a preference.
 *
 * **One stored width, still.** The panel briefly had two (a document's and the agent
 * browser's) and that was collapsed; the browser's return as a surface does not bring it
 * back. One panel, one width the operator drags, and the open set
 * contributes a *floor* rather than a remembered number of its own. A per-surface map
 * would mean the panel changing size as surfaces come and go, which is the panel moving
 * under the operator rather than for them.
 *
 * **The floor is derived, the widen is a one-shot.** A surface arriving into a panel too
 * narrow for it widens the panel once, to its resting width; after that the operator's
 * drag is the only thing that moves it. Sticky widening would mean a surface opened and
 * closed repeatedly ratcheting the panel wider each time.
 */

import { createSignal, onCleanup } from "solid-js";
import { readLS, writeLS } from "~/lib/storage";
import { isPanelSpec, surfaceSpec, type SurfaceId } from "./surfaces";

/** Legacy (and still current) global panel width key. */
const WIDTH_KEY = "heph.chat.viewport.w";

const WIDTH_DEFAULT = 384;
/** The floor when nothing is open — no surface's minimum to defer to. */
const WIDTH_MIN = 320;
/** The widest the panel may be asked for. The *effective* max is also bounded by the
 *  row (see `ceiling`), so this is a preference cap rather than a layout one. */
const WIDTH_CEILING = 1200;
/** Room the conversation column keeps however wide the panel is dragged — below this
 *  the transcript stops being a transcript and becomes a gutter. */
const TRANSCRIPT_MIN = 480;

/** How much width the panel's row actually has, reactively — set by the screen that
 *  owns the row (`ChatRoomScreen`, from a `ResizeObserver` on it).
 *
 *  It has to be the **row**, not the window: by the time the layout reaches here the
 *  nav rail and the shell's padding are already spent, so clamping against
 *  `window.innerWidth` reserves a transcript that isn't there and lets the panel take
 *  ~300px more than the row can give. `Infinity` until the first measurement, so the
 *  ceiling stands alone rather than guessing at a box nobody has measured yet. */
const [availableWidth, setAvailableWidth] = createSignal(Infinity);

export { setAvailableWidth };

/** The panel region's own measured box — what the splitting policy asks "does this
 *  fit?" of. Distinct from the row: the strips and the panel's chrome are already
 *  spent by the time the panels get their space, and a policy that split against the
 *  row would promise room the panes do not have. */
const [panelBox, setPanelBox] = createSignal({ width: 0, height: 0 });

export { panelBox };

/** Publish the panel region's box and keep it current. Call from `onMount` with the
 *  element the panes are laid out in. */
export function observePanelBox(el: HTMLElement): void {
  const read = (): void => {
    setPanelBox({ width: el.clientWidth, height: el.clientHeight });
  };
  read();
  const observer = new ResizeObserver(read);
  observer.observe(el);
  onCleanup(() => observer.disconnect());
}

/** The narrowest the panel may be while every open panel surface stays legible. A
 *  layout with nothing panel-shaped in it falls back to the bare minimum. */
export function floorFor(open: readonly SurfaceId[]): number {
  let floor = WIDTH_MIN;
  for (const id of open) {
    const spec = surfaceSpec(id);
    if (isPanelSpec(spec)) floor = Math.max(floor, spec.minWidth);
  }
  return floor;
}

/** The resting width a surface wants when it arrives — the widest of the open set. */
export function restingWidthFor(open: readonly SurfaceId[]): number {
  let want = WIDTH_DEFAULT;
  for (const id of open) {
    const spec = surfaceSpec(id);
    if (isPanelSpec(spec)) want = Math.max(want, spec.defaultWidth);
  }
  return want;
}

/** The widest the panel may be right now: its own ceiling, less what the row cannot
 *  spare. Floored at `WIDTH_MIN` so it can never invert. */
function ceiling(): number {
  return Math.max(
    WIDTH_MIN,
    Math.min(WIDTH_CEILING, availableWidth() - TRANSCRIPT_MIN),
  );
}

/** Clamps a candidate width to the draggable range for the currently open set —
 *  exported so a live drag can apply the same bounds per pointermove tick without
 *  persisting until the drag settles.
 *
 *  Floor and ceiling cannot fight: the ceiling wins, so on a row too narrow to honour
 *  a surface's minimum the panel takes what there is rather than pushing the
 *  transcript off the edge. */
export const clampWidth = (
  w: number,
  open: readonly SurfaceId[] = [],
): number => {
  const max = ceiling();
  return Math.min(max, Math.max(Math.min(floorFor(open), max), w));
};

/** The stored *preference*, unclamped — clamping happens on read so a width set on a
 *  wide display isn't permanently trimmed by one narrow session. */
const [storedWidth, setStoredWidth] = createSignal(
  Number(readLS(WIDTH_KEY)) || WIDTH_DEFAULT,
);

/** The panel's global (cross-thread) width, given what is open in it. */
export function panelWidth(open: readonly SurfaceId[] = []): number {
  return clampWidth(storedWidth(), open);
}

export function setPanelWidth(
  w: number,
  open: readonly SurfaceId[] = [],
): void {
  const clamped = clampWidth(w, open);
  setStoredWidth(clamped);
  writeLS(WIDTH_KEY, String(clamped));
}

/** Widen the panel to hold `open` comfortably, once, if it is currently narrower.
 *  Returns whether it moved — the caller need not care, but a test does. */
export function widenFor(open: readonly SurfaceId[]): boolean {
  const want = restingWidthFor(open);
  if (storedWidth() >= want) return false;
  setPanelWidth(want, open);
  return true;
}
