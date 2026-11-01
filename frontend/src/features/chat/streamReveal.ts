/**
 * Scheduling for the answer's per-character reveal.
 *
 * Two things make this harder than "fade in the new text".
 *
 * **The DOM is rebuilt on every delta.** `Markdown streamStable` keeps settled
 * blocks but re-renders the trailing one, so any element mid-animation is
 * destroyed and replaced. Animating "whatever arrived since last time" therefore
 * truncates every fade at the next delta: a run appears dim, snaps to full
 * opacity a few frames later, and the next run starts over. So characters are
 * scheduled against **absolute wall-clock times** that survive the rebuild — on
 * re-render a character's `animation-delay` is recomputed as `start - now`,
 * negative for one already in flight, which CSS reads as "begin partway
 * through". The fade continues as if nothing happened.
 *
 * **The arrival rate varies by an order of magnitude.** A fixed per-character
 * step cannot be right for both: fast enough for 300 chars/s and the reveal is
 * invisible at 30; slow enough to see at 30 and each delta's wave is still
 * running when the next three have landed, which puts later characters ahead of
 * earlier ones and scrambles the order outright. So the step is **paced to the
 * observed interval between deltas** — one run of characters is spread over
 * roughly the time until the next run is expected. The reveal front then travels
 * at whatever speed the model is producing, and it looks the same at any of
 * them, which is the property worth having.
 *
 * Everything here is pure so it can be reasoned about (and tested) without a
 * DOM; `TurnBlocks` owns the wrapping.
 */

/** How long one character takes to resolve. Owned here rather than in CSS: the
 *  schedule has to know it to decide when a character is finished, and two
 *  copies of the number would silently drift. Applied inline as `--reveal-ms`.
 *
 *  This is over the human register's 240ms ceiling, deliberately, as that
 *  register's one stated exception (§8). The ceiling governs anything the
 *  operator is *waiting on* — a control answering a click, an overlay opening —
 *  where a long duration reads as lag. Nothing waits on this: it is paced to a
 *  stream that already takes seconds, and a character is legible well before it
 *  finishes settling. Under the ceiling the resolve is over before the eye
 *  reaches it, and the effect is spent on nobody. */
export const REVEAL_MS = 320;

/** Bounds on the per-character step. The floor keeps a burst from collapsing
 *  into a single flash; the ceiling keeps a trickle from looking like a
 *  typewriter, which is a different effect with a different meaning. */
const STEP_MIN = 3;
const STEP_MAX = 26;

/** Bounds on the tracked delta interval. The floor stops sub-frame pacing from
 *  driving the step to nothing; the ceiling stops one long pause — a tool call
 *  mid-answer — from stretching the run after it into a crawl. */
const INTERVAL_MIN = 24;
const INTERVAL_MAX = 280;

/** Where the tracked interval starts, before anything has been observed. */
export const INTERVAL_SEED = 90;

/** How much of a new observation to take. Low enough that one late delta does
 *  not swing the pacing, high enough to follow a genuine change in rate. */
const SMOOTHING = 0.3;

/** Fold an observed gap between deltas into the running estimate. */
export function nextInterval(prev: number, observed: number): number {
  const clamped = Math.min(INTERVAL_MAX, Math.max(INTERVAL_MIN, observed));
  return prev * (1 - SMOOTHING) + clamped * SMOOTHING;
}

/**
 * Start times for characters `[from, to)`, staggered from `now` so the run
 * resolves left-to-right as a wave rather than as a block.
 *
 * The step is `interval / count` — the run is spread over about the time until
 * the next one is expected, so the reveal front moves at the arrival rate and
 * has just finished when more text lands. That ratio is what makes it look the
 * same whether the model is producing 30 characters a second or 300.
 *
 * A burst too large to stagger inside the interval at `STEP_MIN` has its
 * overflow resolve together, at `now`. That bounds how far the reveal can trail
 * the text that has actually arrived — the caret sits at the true end of the
 * text, so a reveal that lagged would strand it ahead of anything visible.
 *
 * **Callers must re-anchor the not-yet-started tail** rather than appending:
 * pacing changes as the rate does, and a character that has not begun is
 * invisible, so moving it is free. Appending instead would let a fast delta
 * schedule characters *before* ones already queued, which both scrambles the
 * order and breaks the non-decreasing invariant `firstLiveIndex` relies on.
 */
export function paceReveal(
  from: number,
  to: number,
  now: number,
  interval: number,
): number[] {
  const n = to - from;
  if (n <= 0) return [];
  const budget = Math.min(INTERVAL_MAX, Math.max(INTERVAL_MIN, interval));
  const staggered = Math.min(n, Math.max(1, Math.floor(budget / STEP_MIN)));
  const immediate = n - staggered;
  const step = Math.min(STEP_MAX, Math.max(STEP_MIN, budget / staggered));
  return Array.from({ length: n }, (_, i) =>
    i < immediate ? now : now + (i - immediate) * step,
  );
}

/**
 * Extend a schedule to cover `count` characters, re-pacing the tail that has not
 * begun. **This is the protocol `paceReveal` requires** — it lives here rather
 * than at the call site so the invariant is testable without a DOM, and so there
 * is one place to get it right.
 */
export function extendSchedule(
  starts: number[],
  count: number,
  now: number,
  interval: number,
): number[] {
  const begun = startedCount(starts, now);
  if (count <= begun) return starts;
  return starts.slice(0, begun).concat(paceReveal(begun, count, now, interval));
}

/**
 * The `animation-delay` for a character scheduled at `start`, or `null` when its
 * reveal has already finished and it should render as plain text.
 *
 * Negative means "already in flight" — the character resumes at the right phase
 * instead of restarting, which is the whole point of scheduling in absolute
 * time. Positive means "not yet begun": with `animation-fill-mode: both` it
 * holds at the keyframes' `from` state, invisible, until its moment.
 */
export function revealDelay(start: number, now: number): number | null {
  return start + REVEAL_MS <= now ? null : start - now;
}

/** Characters starting within one 144 Hz frame share a wrapper; their fades are indistinguishable. */
export const SHARED_SPAN_MS = 1000 / 144;

/** Characters up to `end` as one node: a wrapper with `delay`, or plain text when null. */
export interface RevealRun {
  end: number;
  delay: number | null;
}

/** Characters `[from, to)` as runs; settled or unscheduled ones are plain and land hard. */
export function revealRuns(
  starts: number[],
  from: number,
  to: number,
  now: number,
): RevealRun[] {
  const delayAt = (k: number): number | null =>
    k < starts.length ? revealDelay(starts[k], now) : null;
  const runs: RevealRun[] = [];
  let i = from;
  while (i < to) {
    const delay = delayAt(i);
    let end = i + 1;
    if (delay === null) while (end < to && delayAt(end) === null) end++;
    else
      while (
        end < to &&
        end < starts.length &&
        starts[end] - starts[i] < SHARED_SPAN_MS
      )
        end++;
    runs.push({ end, delay });
    i = end;
  }
  return runs;
}

/**
 * How many scheduled characters have already begun resolving. Everything from
 * here on is still invisible and can be re-paced freely; everything before it
 * must keep the start time it was given, or its fade would jump.
 */
export function startedCount(starts: number[], now: number): number {
  return lowerBound(starts, (start) => start <= now);
}

/**
 * The first character index that still has an animation to run. Everything
 * before it is settled and needs no wrapper — which is what keeps the number of
 * spans bounded to the live window rather than growing with the answer.
 */
export function firstLiveIndex(starts: number[], now: number): number {
  return lowerBound(starts, (start) => start + REVEAL_MS <= now);
}

/** First index whose start does NOT satisfy `done`. Valid because `starts` is
 *  non-decreasing — see the re-anchoring note on `paceReveal`. */
function lowerBound(
  starts: number[],
  done: (start: number) => boolean,
): number {
  let lo = 0;
  let hi = starts.length;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (done(starts[mid])) lo = mid + 1;
    else hi = mid;
  }
  return lo;
}
