// Pin the suite's timezone, the same reason `lib/format/index.test.ts` does: `bun test`
// runs at UTC, and UTC is the one zone where reading a zoneless wire timestamp as local
// gives the right answer anyway. Without this the naive-stamp cases below pass whether
// or not the code parses correctly, which is the same as not having written them.
process.env.TZ = "America/New_York";

import { describe, expect, test } from "bun:test";
import type { ChatSummary } from "./model";
import { COLD_AFTER_MS, showThreadRecap } from "./recapVisibility";

/**
 * Whether the re-entry band is drawn.
 *
 * Each case below is a way the band used to be wrong, or a way it would be wrong if a
 * condition were dropped. The fixtures fight the rule: every one of them satisfies
 * every condition except the one under test, so deleting that condition makes the test
 * fail rather than making another condition carry it.
 */

const NOW = Date.parse("2026-09-20T18:00:00Z");
/** Comfortably past the threshold — the band's ordinary case. */
const LONG_AGO = new Date(NOW - COLD_AFTER_MS * 3).toISOString();

function thread(over: Partial<ChatSummary> = {}): ChatSummary {
  return {
    id: "c1",
    title: "Fix the scroll",
    updatedAt: LONG_AGO,
    createdAt: LONG_AGO,
    messageCount: 8,
    workSummary:
      "Traced the sticky transcript to its follow rule and rewrote it.",
    workspaceKind: "sandbox",
    ...over,
  };
}

const at = (summary: ChatSummary | undefined, streaming = false): boolean =>
  showThreadRecap(summary, { streaming, now: NOW });

describe("the band is drawn for a cold thread that has something to say", () => {
  test("an idle thread with a summary shows it", () => {
    expect(at(thread())).toBe(true);
  });

  test("no thread at all shows nothing", () => {
    expect(at(undefined)).toBe(false);
  });
});

describe("and is withheld in every case where it would be noise", () => {
  test("no summary written yet — the ordinary state of a working thread", () => {
    expect(at(thread({ workSummary: undefined }))).toBe(false);
  });

  test("an empty summary is not a summary", () => {
    expect(at(thread({ workSummary: "" }))).toBe(false);
  });

  test("spoken to a minute ago — the transcript is still the answer", () => {
    const recent = new Date(NOW - 60_000).toISOString();
    expect(at(thread({ updatedAt: recent }))).toBe(false);
  });

  test("one minute short of the threshold still withholds", () => {
    const nearly = new Date(NOW - COLD_AFTER_MS + 60_000).toISOString();
    expect(at(thread({ updatedAt: nearly }))).toBe(false);
  });

  test("one minute past it shows", () => {
    const just = new Date(NOW - COLD_AFTER_MS - 60_000).toISOString();
    expect(at(thread({ updatedAt: just }))).toBe(true);
  });

  test("a run streaming in this room", () => {
    expect(at(thread(), true)).toBe(false);
  });

  test("a run driven from somewhere else", () => {
    // `streaming` only knows about the room the operator is looking at; a thread can
    // be running because a scheduled task or another window set it going.
    expect(at(thread({ activity: "running" }))).toBe(false);
  });

  test("a thread parked on an approval is live, not cold", () => {
    expect(at(thread({ activity: "awaiting_input" }))).toBe(false);
  });
});

describe("the wire's naive timestamps are read as UTC", () => {
  // How this was actually found: the band never appeared in the running product. The
  // backend serializes `2026-09-20T19:47:23.208699` with no `Z`, `Date.parse` reads that
  // as *local*, and on a UTC−4 host a thread last spoken to two hours ago came out 107
  // minutes in the FUTURE — so the subtraction went negative and the gate never opened.
  // These fixtures carry no zone on purpose; with `new Date()` in place of
  // `parseInstant` the first one fails anywhere west of Greenwich.
  const naive = (msAgo: number): string =>
    new Date(NOW - msAgo).toISOString().replace("Z", "");

  test("a zoneless stamp well past the threshold shows", () => {
    expect(at(thread({ updatedAt: naive(COLD_AFTER_MS * 3) }))).toBe(true);
  });

  test("a zoneless stamp inside the threshold still withholds", () => {
    expect(at(thread({ updatedAt: naive(60_000) }))).toBe(false);
  });
});

describe("an age it cannot read is not an age", () => {
  test("an unparseable stamp withholds rather than falling through", () => {
    // `parseInstant` answers with NaN instead of throwing, and every comparison
    // against NaN is false — so a bare `< COLD_AFTER_MS` check passes a thread of
    // unknown age straight through the gate that exists to establish its age.
    expect(at(thread({ updatedAt: "not a date" }))).toBe(false);
  });

  test("an empty stamp does too", () => {
    expect(at(thread({ updatedAt: "" }))).toBe(false);
  });
});

describe("a finished run is not a live one", () => {
  test("a thread whose last run failed still shows its recap", () => {
    // The failure is exactly what an operator coming back needs to see, and
    // `lastOutcome` is a terminal state — reading it as "live" would hide the band on
    // precisely the threads it matters most for.
    expect(at(thread({ lastOutcome: "error" }))).toBe(true);
  });
});
