import { describe, expect, test } from "bun:test";
import {
  OLDER_GROUP,
  PINNED_GROUP,
  groupSessions,
  rowAge,
  type SessionGroup,
  type WorkspaceDirectory,
} from "./sessionGroups";
import type { ChatSummary } from "./model";

function session(id: string, extra: Partial<ChatSummary> = {}): ChatSummary {
  return {
    id,
    title: id,
    updatedAt: "2026-08-30T00:00:00Z",
    // Deliberately earlier than `updatedAt`, and irrelevant to grouping: these
    // rows are filed by recency of the last turn, never by when the thread opened.
    createdAt: "2026-08-01T00:00:00Z",
    messageCount: 2,
    workspaceKind: "sandbox",
    ...extra,
  };
}

function dir(id: string, label = id): WorkspaceDirectory {
  return { id, label };
}

/** The project behind each heading. Asserts the kind on the way through rather than
 *  reaching past it, so a recency group turning up here is a real failure and reads as
 *  one instead of as `undefined` in a diff. */
function projectIds(groups: SessionGroup[]): string[] {
  return groups.map((g) => {
    if (g.kind !== "directory")
      throw new Error(`expected a directory group, got ${g.kind}`);
    return g.projectId;
  });
}

/** A fixed vantage point. The bucket boundaries are the behaviour, so the tests have
 *  to stand somewhere known — mid-afternoon, so "earlier today" and "24 hours ago" are
 *  genuinely different days rather than the same one by luck. */
const NOW = new Date(2026, 8, 10, 15, 0);

/** `n` days before NOW, at noon — clear of both midnight boundaries. */
function daysAgo(n: number): string {
  return new Date(2026, 8, 10 - n, 12, 0).toISOString();
}

describe("groupSessions", () => {
  test("threads with no project file by recency", () => {
    // They have no directory to be filed under, so the only heading worth drawing is
    // when they were last touched.
    const groups = groupSessions(
      [
        session("a", { updatedAt: daysAgo(0) }),
        session("b", { updatedAt: daysAgo(1) }),
        session("c", { updatedAt: daysAgo(3) }),
      ],
      { now: NOW },
    );
    expect(groups.map((g) => g.label)).toEqual([
      "Today",
      "Yesterday",
      "Previous 7 days",
    ]);
    // Captions, not places: nothing to fold, nothing to start a thread in.
    expect(groups.every((g) => g.kind === "recency")).toBe(true);
  });

  test("recency buckets run from today out to a dated month", () => {
    const groups = groupSessions(
      [
        session("today", { updatedAt: daysAgo(0) }),
        session("yesterday", { updatedAt: daysAgo(1) }),
        session("week", { updatedAt: daysAgo(6) }),
        session("month", { updatedAt: daysAgo(25) }),
        // Same calendar year: the bare month is unambiguous.
        session("july", { updatedAt: daysAgo(60) }),
        // A previous year has to carry it, or two Julys collide.
        session("old", {
          updatedAt: new Date(2024, 2, 4, 12).toISOString(),
        }),
      ],
      { now: NOW },
    );
    expect(groups.map((g) => g.label)).toEqual([
      "Today",
      "Yesterday",
      "Previous 7 days",
      "Previous 30 days",
      "July",
      "March 2024",
    ]);
  });

  test("today starts at local midnight, not 24 hours back", () => {
    // The case the naive "now minus 24h" gets wrong: a thread from yesterday evening
    // would read as Today every morning.
    const groups = groupSessions(
      [
        session("early", {
          updatedAt: new Date(2026, 8, 10, 0, 30).toISOString(),
        }),
        session("last-night", {
          updatedAt: new Date(2026, 8, 9, 23, 30).toISOString(),
        }),
      ],
      { now: NOW },
    );
    expect(groups.map((g) => g.label)).toEqual(["Today", "Yesterday"]);
  });

  test("bucket edges land on local midnight across a DST change", () => {
    // US DST began 2026-03-08. Standing on the 12th, the 7-day edge is midnight on the
    // 5th — but reached by subtracting 7 × 86,400,000ms it lands at 23:00 on the 4th,
    // which pulls a thread from just after midnight on the 5th into the wrong bucket.
    // Only meaningful where the runner observes DST; elsewhere it simply asserts the
    // ordinary boundary.
    const groups = groupSessions(
      [
        session("edge", {
          updatedAt: new Date(2026, 2, 5, 0, 30).toISOString(),
        }),
      ],
      { now: new Date(2026, 2, 12, 15, 0) },
    );
    expect(groups.map((g) => g.label)).toEqual(["Previous 7 days"]);
  });

  test("pinned threads come out into their own run, ahead of the rest", () => {
    // `orderSessions` has already floated them, so the group is created first and leads
    // the list. A pin that merely sorted to the top of Today was indistinguishable from
    // anything else touched today.
    const groups = groupSessions(
      [
        session("kept", { updatedAt: daysAgo(40) }),
        session("today", { updatedAt: daysAgo(0) }),
      ],
      { now: NOW, pinned: new Set(["kept"]) },
    );
    expect(groups.map((g) => g.label)).toEqual([PINNED_GROUP, "Today"]);
    expect(groups[0].sessions.map((s) => s.id)).toEqual(["kept"]);
  });

  test("an unparseable stamp sorts with the oldest rather than throwing", () => {
    const groups = groupSessions(
      [session("bad", { updatedAt: "not a date" })],
      { now: NOW },
    );
    expect(groups.map((g) => g.label)).toEqual([OLDER_GROUP]);
  });

  test("recency buckets never repeat or arrive empty", () => {
    const rows = [
      session("a", { updatedAt: daysAgo(0) }),
      session("b", { updatedAt: daysAgo(0) }),
      session("c", { updatedAt: daysAgo(2) }),
    ];
    const groups = groupSessions(rows, { now: NOW });
    expect(groups.map((g) => g.label)).toEqual(["Today", "Previous 7 days"]);
    expect(groups.every((g) => g.sessions.length > 0)).toBe(true);
    expect(groups.flatMap((g) => g.sessions)).toHaveLength(rows.length);
  });

  test("an empty list produces no groups at all", () => {
    // Not one empty section: the caller renders its own "no threads" state, and a
    // headerless empty run would draw nothing while still occupying a slot.
    expect(groupSessions([])).toEqual([]);
  });

  test("threads with a project group under it", () => {
    const groups = groupSessions(
      [
        session("a", { projectId: "p1" }),
        session("b", { projectId: "p2" }),
        session("c", { projectId: "p1" }),
      ],
      { directories: [dir("p1", "acme-api"), dir("p2", "odysseus")] },
    );
    expect(groups.map((g) => g.label)).toEqual(["acme-api", "odysseus"]);
    expect(groups[0].sessions.map((s) => s.id)).toEqual(["a", "c"]);
    expect(groups[1].sessions.map((s) => s.id)).toEqual(["b"]);
  });

  test("two directories sharing a basename stay two groups", () => {
    // The whole reason filing is by project id. Two repositories both holding a
    // `frontend` are two places to work, and merging them would point one section's
    // controls at whichever won.
    const groups = groupSessions(
      [session("a", { projectId: "p1" }), session("b", { projectId: "p2" })],
      { directories: [dir("p1", "acme/frontend"), dir("p2", "beta/frontend")] },
    );
    expect(groups).toHaveLength(2);
    expect(projectIds(groups)).toEqual(["p1", "p2"]);
  });

  test("a directory with no threads still gets a section", () => {
    // The directory comes first — it is where a thread is started from, so an empty
    // one is the ordinary beginning of a piece of work, not a dead row.
    const groups = groupSessions([], {
      directories: [dir("p1", "acme-api")],
    });
    expect(groups).toHaveLength(1);
    expect(groups[0].label).toBe("acme-api");
    expect(groups[0].sessions).toEqual([]);
  });

  test("empty directories trail the ones holding threads", () => {
    // A section with nothing in it has nothing to say about how recently the operator
    // was there, so it cannot claim a place among the sections that do.
    const groups = groupSessions(
      [session("a", { projectId: "p2" })],
      // Listing order deliberately puts the empty one first — the rule has to beat it.
      { directories: [dir("p1", "empty"), dir("p2", "worked-in")] },
    );
    expect(groups.map((g) => g.label)).toEqual(["worked-in", "empty"]);
  });

  test("groups holding threads appear in the order their first thread does", () => {
    // The input arrives pinned-first-then-newest, and that ordering has to carry
    // through: a pinned thread floats its whole workspace to the top. One rule,
    // visible in the rows, rather than a second sort the operator cannot see.
    const groups = groupSessions(
      [
        session("pinned", { projectId: "p1" }),
        session("recent", { projectId: "p2" }),
        session("older", { projectId: "p2" }),
      ],
      // Listing order is the opposite, so declaration order cannot be what passes this.
      { directories: [dir("p2", "busy-repo"), dir("p1", "quiet-repo")] },
    );
    expect(groups.map((g) => g.label)).toEqual(["quiet-repo", "busy-repo"]);
  });

  test("project threads and recency threads share one list, in first-thread order", () => {
    const groups = groupSessions(
      [
        session("a", { updatedAt: daysAgo(0) }),
        session("b", { projectId: "p1", updatedAt: daysAgo(0) }),
        session("c", { updatedAt: daysAgo(2) }),
      ],
      { directories: [dir("p1", "acme-api")], now: NOW },
    );
    expect(groups.map((g) => [g.kind, g.label])).toEqual([
      ["recency", "Today"],
      ["directory", "acme-api"],
      ["recency", "Previous 7 days"],
    ]);
  });

  test("a pin lifts a projectless thread out but leaves a project thread in place", () => {
    const groups = groupSessions(
      [session("pinned-project", { projectId: "p1" }), session("pinned-loose")],
      {
        directories: [dir("p1", "acme-api")],
        pinned: new Set(["pinned-project", "pinned-loose"]),
        now: NOW,
      },
    );
    expect(groups.map((g) => g.label)).toEqual(["acme-api", PINNED_GROUP]);
  });

  test("a thread whose directory is not listed is left out", () => {
    // The archived case. Archiving is the operator saying they no longer work there;
    // sweeping the directory's threads loose into the list would honour the letter of
    // that and none of the intent. Nothing is lost — un-archiving brings both back.
    const groups = groupSessions(
      [
        session("archived", { projectId: "gone" }),
        session("kept", { updatedAt: daysAgo(0) }),
      ],
      { directories: [], now: NOW },
    );
    expect(groups.map((g) => g.label)).toEqual(["Today"]);
    expect(groups[0].sessions.map((s) => s.id)).toEqual(["kept"]);
  });

  test("every listed thread survives the partition", () => {
    const rows = [
      session("a", { projectId: "p1" }),
      session("b", { updatedAt: daysAgo(0) }),
      session("c", { projectId: "p2" }),
      session("d", { projectId: "p1" }),
    ];
    const flattened = groupSessions(rows, {
      directories: [dir("p1"), dir("p2")],
      now: NOW,
    }).flatMap((g) => g.sessions);
    expect(flattened).toHaveLength(rows.length);
    expect(new Set(flattened.map((s) => s.id)).size).toBe(rows.length);
  });
});

describe("rowAge", () => {
  const now = new Date("2026-10-10T12:00:00Z");
  const ago = (ms: number) => new Date(now.getTime() - ms).toISOString();
  const MIN = 60_000;

  test("steps from minutes to hours to days, flooring each", () => {
    expect(rowAge(ago(20_000), now)).toBe("now");
    expect(rowAge(ago(2 * MIN), now)).toBe("2m");
    expect(rowAge(ago(59 * MIN), now)).toBe("59m");
    expect(rowAge(ago(60 * MIN), now)).toBe("1h");
    expect(rowAge(ago(4 * 60 * MIN + 59 * MIN), now)).toBe("4h");
    expect(rowAge(ago(24 * 60 * MIN), now)).toBe("1d");
  });

  test("says nothing at 30 days and past, where the heading names the month", () => {
    expect(rowAge(ago(29 * 24 * 60 * MIN), now)).toBe("29d");
    expect(rowAge(ago(30 * 24 * 60 * MIN), now)).toBeUndefined();
    expect(rowAge(ago(400 * 24 * 60 * MIN), now)).toBeUndefined();
  });

  test("an unparseable stamp has no age", () => {
    expect(rowAge("not a date", now)).toBeUndefined();
  });
});
