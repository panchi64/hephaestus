/**
 * The decisions behind how the transcript scrolls: whether the view follows the bottom,
 * which element it holds still while layout changes around it, and the space a send
 * reserves so the operator's message can sit at the top of the view.
 *
 * Pure, so the DOM half in `transcriptScroll.ts` only measures and writes.
 */

/** Upward movement under this many pixels is rounding, not a gesture. */
const DRIFT_PX = 2;

/** Where the container was, and where it is now. */
export interface FollowReading {
  /** Whether the follow is currently attached. */
  pinned: boolean;
  /** Whether the operator is scrolling: input on the transcript just now, or a held
   *  pointer (a scrollbar drag). */
  operator: boolean;
  /** `scrollTop` as of the previous reading — or as of the DOM half's own last write. */
  lastTop: number;
  /** `scrollHeight` as of the previous reading. */
  lastHeight: number;
  top: number;
  height: number;
}

/**
 * Whether the follow should still be attached, given how the container just moved.
 *
 * An upward move the operator made lets go, with no minimum gesture. An upward move
 * while the content shrank is the browser clamping after a fold closed, and one nobody
 * made is the browser too; neither lets go. Nothing here re-attaches.
 */
export function nextPinned(reading: FollowReading): boolean {
  const shrank = reading.height < reading.lastHeight;
  const movedUp = reading.top < reading.lastTop - DRIFT_PX;
  if (reading.operator && !shrank && movedUp) return false;
  return reading.pinned;
}

/** Whether the view follows the bottom, and whether the operator asked it to. */
export interface FollowState {
  pinned: boolean;
  /** Set by the jump control. A send keeps a follow the operator chose and lets go of
   *  one that came from opening the thread. */
  chosen: boolean;
}

/**
 * - `open`: a thread was opened.
 * - `jump`: the view was sent to the end without the operator asking to follow it.
 * - `follow`: the operator took the jump control.
 * - `letGo`: the operator scrolled up, or the view was sent to one turn.
 * - `send`: a run started.
 * - `resume`: a run already under way was picked up; the follow stays as it was.
 */
export type FollowEvent =
  "open" | "jump" | "follow" | "letGo" | "send" | "resume";

export function nextFollow(
  state: FollowState,
  event: FollowEvent,
): FollowState {
  switch (event) {
    case "open":
      return { pinned: true, chosen: false };
    case "jump":
      return { pinned: true, chosen: state.chosen };
    case "follow":
      return { pinned: true, chosen: true };
    case "letGo":
      return { pinned: false, chosen: false };
    case "send":
      return state.chosen ? state : { pinned: false, chosen: false };
    case "resume":
      return state;
  }
}

/** A candidate's viewport box, and whether it is the answer still being written. */
export interface AnchorCandidate {
  top: number;
  bottom: number;
  live: boolean;
}

/**
 * The index of the candidate to hold still, or -1 when none is on screen.
 *
 * The live answer wins whenever any of it is visible, because it is what the operator
 * is reading and everything that resizes mid-run (reasoning, folds, the progress rail)
 * sits above it. Otherwise the first candidate that starts inside the view, then the
 * first that reaches into it from above.
 */
export function pickAnchor(
  candidates: AnchorCandidate[],
  viewTop: number,
  viewBottom: number,
): number {
  const visible = (c: AnchorCandidate): boolean =>
    c.bottom > viewTop && c.top < viewBottom;
  const live = candidates.findIndex((c) => c.live && visible(c));
  if (live >= 0) return live;
  const starting = candidates.findIndex(
    (c) => c.top >= viewTop && c.top < viewBottom,
  );
  if (starting >= 0) return starting;
  return candidates.findIndex(visible);
}

/**
 * The index of the first of `count` stacked boxes whose bottom is below `top`, or
 * `count` when none is. `bottom` must be non-decreasing in its index.
 */
export function firstReaching(
  count: number,
  bottom: (i: number) => number,
  top: number,
): number {
  let lo = 0;
  let hi = count;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (bottom(mid) <= top) lo = mid + 1;
    else hi = mid;
  }
  return lo;
}

/**
 * The minimum content height that lets a turn starting `offset` px into the content be
 * scrolled to the top of a view `clientHeight` tall with the given vertical insets.
 */
export function roomFor(
  offset: number,
  clientHeight: number,
  insetTop: number,
  insetBottom: number,
): number {
  return Math.max(0, offset + clientHeight - insetTop - insetBottom);
}
