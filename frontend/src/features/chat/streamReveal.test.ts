import { describe, expect, test } from "bun:test";
import {
  INTERVAL_SEED,
  REVEAL_MS,
  extendSchedule,
  firstLiveIndex,
  nextInterval,
  paceReveal,
  SHARED_SPAN_MS,
  revealDelay,
  revealRuns,
  startedCount,
} from "./streamReveal";

/** Replays a stream at a fixed rate through the real scheduler and returns the
 *  schedule it ends up with. This is the only way to exercise the invariant that
 *  actually matters — the schedule is built up over many deltas, and the bug it
 *  guards against only appears across them. */
function replay(
  charsPerDelta: number,
  deltaMs: number,
  deltas: number,
): number[] {
  let starts: number[] = [];
  let interval = INTERVAL_SEED;
  let last = 0;
  for (let d = 1; d <= deltas; d++) {
    const now = d * deltaMs;
    if (last) interval = nextInterval(interval, now - last);
    last = now;
    starts = extendSchedule(starts, d * charsPerDelta, now, interval);
  }
  return starts;
}

function isNonDecreasing(xs: number[]): boolean {
  return xs.every((x, i) => i === 0 || x >= xs[i - 1]);
}

describe("paceReveal", () => {
  test("staggers a run so it resolves left to right", () => {
    const starts = paceReveal(0, 4, 1000, 100);
    expect(starts).toHaveLength(4);
    // Strictly increasing is what makes it a wave rather than a block; a flat
    // schedule would be the chunked behaviour this replaced.
    for (let i = 1; i < starts.length; i++) {
      expect(starts[i]).toBeGreaterThan(starts[i - 1]);
    }
    expect(starts[0]).toBe(1000);
  });

  test("spreads a run over roughly the interval until the next one", () => {
    // The core ratio: a delta's characters should be revealed by about the time
    // the next delta lands, so the front moves at the arrival rate.
    const starts = paceReveal(0, 10, 0, 200);
    const spread = starts[starts.length - 1] - starts[0];
    expect(spread).toBeGreaterThan(140);
    expect(spread).toBeLessThanOrEqual(200);
  });

  test("a faster stream uses a smaller step, so the front keeps pace", () => {
    const slow = paceReveal(0, 8, 0, 240);
    const fast = paceReveal(0, 8, 0, 40);
    expect(fast[1] - fast[0]).toBeLessThan(slow[1] - slow[0]);
  });

  test("holds the step inside its bounds at both extremes", () => {
    // A trickle must not become a typewriter...
    const trickle = paceReveal(0, 2, 0, 5000);
    expect(trickle[1] - trickle[0]).toBeLessThanOrEqual(26);
    // ...and a firehose must not collapse into a single flash.
    const flood = paceReveal(0, 400, 0, 30);
    const stepped = flood.filter((s) => s > 0);
    expect(stepped.length).toBeGreaterThan(1);
    for (let i = 1; i < stepped.length; i++) {
      expect(stepped[i] - stepped[i - 1]).toBeGreaterThanOrEqual(3);
    }
  });

  test("a burst too large for its interval resolves its overflow together", () => {
    // Bounded lag: without this the reveal trails arrival further and further,
    // and the caret — which sits at the true end of the text — is left ahead of
    // anything visible.
    const starts = paceReveal(0, 300, 1000, 60);
    const spread = starts[starts.length - 1] - starts[0];
    expect(spread).toBeLessThanOrEqual(280);
    expect(starts.filter((s) => s === 1000).length).toBeGreaterThan(1);
  });

  test("is non-decreasing for every run it produces", () => {
    for (const n of [1, 2, 7, 40, 300]) {
      for (const interval of [24, 60, 120, 280]) {
        expect(isNonDecreasing(paceReveal(0, n, 0, interval))).toBe(true);
      }
    }
  });

  test("an empty range schedules nothing", () => {
    expect(paceReveal(7, 7, 0, 100)).toEqual([]);
    expect(paceReveal(9, 3, 0, 100)).toEqual([]);
  });
});

describe("the schedule across many deltas", () => {
  // The invariant everything else depends on. Appending each delta's run at
  // `now` — the obvious implementation — breaks this the moment a delta lands
  // before the previous run has finished staggering, which is the normal case
  // on a fast stream: characters then resolve out of order and the binary
  // searches below silently return nonsense.
  test("stays non-decreasing at every arrival rate", () => {
    for (const [chars, gap] of [
      [1, 200],
      [4, 100],
      [12, 50],
      [40, 30],
      [100, 25],
    ] as const) {
      const starts = replay(chars, gap, 12);
      expect(isNonDecreasing(starts)).toBe(true);
    }
  });

  test("never trails arrival by more than one interval's worth", () => {
    // At a steady rate the last scheduled character should begin within about
    // one delta of the moment its text arrived — that is what "the front moves
    // at the arrival rate" means in practice.
    const gap = 50;
    const deltas = 12;
    const starts = replay(12, gap, deltas);
    const lastArrival = deltas * gap;
    expect(starts[starts.length - 1] - lastArrival).toBeLessThanOrEqual(280);
  });

  test("keeps the start time of any character already in flight", () => {
    // Re-pacing must not disturb a fade in progress; only the invisible tail
    // may move.
    const first = paceReveal(0, 20, 0, 200);
    const inFlight = first.slice(0, startedCount(first, 60));
    expect(inFlight.length).toBeGreaterThan(0);
    const next = extendSchedule(first, 40, 60, 200);
    expect(next.slice(0, inFlight.length)).toEqual(inFlight);
  });
});

describe("nextInterval", () => {
  test("moves toward a new rate without jumping to it", () => {
    const faster = nextInterval(200, 40);
    expect(faster).toBeLessThan(200);
    expect(faster).toBeGreaterThan(40);
  });

  test("converges on a sustained rate", () => {
    let v = INTERVAL_SEED;
    for (let i = 0; i < 40; i++) v = nextInterval(v, 50);
    expect(v).toBeCloseTo(50, 0);
  });

  test("clamps an outlier rather than following it", () => {
    // A long pause mid-answer (a tool call) must not stretch the run after it.
    expect(nextInterval(100, 30_000)).toBeLessThanOrEqual(280);
    // A sub-frame burst must not drive the step to nothing.
    expect(nextInterval(100, 0)).toBeGreaterThanOrEqual(24);
  });
});

describe("revealDelay", () => {
  test("is negative for a character already in flight", () => {
    // This is the fix for the DOM rebuild: a character re-created mid-animation
    // resumes at its phase. A non-negative delay here restarts the fade on
    // every delta, which is the strobing this replaced.
    expect(revealDelay(1000, 1100)).toBe(-100);
  });

  test("is positive for a character whose turn has not come", () => {
    expect(revealDelay(1200, 1000)).toBe(200);
  });

  test("is null once the reveal has finished, so it renders as plain text", () => {
    expect(revealDelay(1000, 1000 + REVEAL_MS)).toBeNull();
    expect(revealDelay(1000, 1000 + REVEAL_MS + 1)).toBeNull();
    expect(revealDelay(1000, 1000 + REVEAL_MS - 1)).not.toBeNull();
  });
});

describe("startedCount / firstLiveIndex", () => {
  // Deliberately ordered so an off-by-one or the wrong predicate gives a
  // different answer than the boundary being tested.
  const starts = [0, 100, 200, 300, 400];

  test("startedCount counts only characters whose moment has passed", () => {
    expect(startedCount(starts, 0)).toBe(1);
    expect(startedCount(starts, 250)).toBe(3);
    expect(startedCount(starts, -1)).toBe(0);
    expect(startedCount(starts, 10_000)).toBe(starts.length);
  });

  test("firstLiveIndex skips every character that has finished resolving", () => {
    // now = 350: only the character starting at 0 is done (0 + 320 <= 350).
    // The one starting at 100 finishes at 420 and is still in flight.
    expect(firstLiveIndex(starts, 350)).toBe(1);
    expect(firstLiveIndex(starts, 420)).toBe(2);
  });

  test("firstLiveIndex returns 0 while nothing has finished", () => {
    expect(firstLiveIndex(starts, 0)).toBe(0);
    expect(firstLiveIndex(starts, REVEAL_MS - 1)).toBe(0);
  });

  test("firstLiveIndex returns the length once everything has settled", () => {
    expect(firstLiveIndex(starts, 10_000)).toBe(starts.length);
  });

  test("both handle an empty schedule", () => {
    expect(firstLiveIndex([], 500)).toBe(0);
    expect(startedCount([], 500)).toBe(0);
  });

  test("agree with a linear scan across a real schedule", () => {
    // The binary searches are only valid because the schedule is
    // non-decreasing; this pins that equivalence rather than trusting it.
    const schedule = replay(6, 60, 10);
    for (let now = 0; now < 1200; now += 7) {
      const live = schedule.findIndex((s) => s + REVEAL_MS > now);
      expect(firstLiveIndex(schedule, now)).toBe(
        live === -1 ? schedule.length : live,
      );
      const pending = schedule.findIndex((s) => s > now);
      expect(startedCount(schedule, now)).toBe(
        pending === -1 ? schedule.length : pending,
      );
    }
  });
});

describe("revealRuns", () => {
  test("characters starting within one frame share a wrapper", () => {
    const now = 1000;
    const starts = [now, now + 3, now + 6, now + 7, now + 20];
    expect(revealRuns(starts, 0, 5, now)).toEqual([
      { end: 3, delay: 0 },
      { end: 4, delay: 7 },
      { end: 5, delay: 20 },
    ]);
  });

  test("a burst starting together is one wrapper", () => {
    const now = 1000;
    const runs = revealRuns(new Array(40).fill(now), 0, 40, now);
    expect(runs).toEqual([{ end: 40, delay: 0 }]);
  });

  test("settled characters join one plain run", () => {
    const now = 1000;
    const done = now - REVEAL_MS - 1;
    const starts = [done, done, done, now - 10, now - 9];
    expect(revealRuns(starts, 0, 5, now)).toEqual([
      { end: 3, delay: null },
      { end: 5, delay: -10 },
    ]);
  });

  test("characters past the schedule land as plain text", () => {
    const now = 1000;
    expect(revealRuns([now, now], 0, 5, now)).toEqual([
      { end: 2, delay: 0 },
      { end: 5, delay: null },
    ]);
  });

  test("covers exactly the requested range", () => {
    const schedule = replay(24, 80, 12);
    const now = 12 * 80;
    const runs = revealRuns(schedule, 30, schedule.length, now);
    expect(runs.at(-1)?.end).toBe(schedule.length);
    runs.forEach((run, i) => {
      const at = i === 0 ? 30 : runs[i - 1].end;
      expect(run.end).toBeGreaterThan(at);
      if (run.delay !== null)
        expect(schedule[run.end - 1] - schedule[at]).toBeLessThan(
          SHARED_SPAN_MS,
        );
    });
  });
});
