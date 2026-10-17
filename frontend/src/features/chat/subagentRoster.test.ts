import { describe, expect, test } from "bun:test";
import type { Subagent } from "./data";
import {
  arrange,
  byUrgency,
  countLabel,
  fullness,
  gaugeTone,
  latestLine,
  rosterCounts,
} from "./subagentRoster";

const agent = (id: string, over: Partial<Subagent> = {}): Subagent => ({
  id,
  conversationId: `c-${id}`,
  runId: `r-${id}`,
  handle: id,
  callsign: "",
  agentClass: "",
  name: "explorer",
  task: "",
  tasks: [],
  status: "running",
  summary: null,
  error: null,
  contextUsed: null,
  contextWindow: null,
  startedAt: "2026-10-16T10:00:00Z",
  endedAt: null,
  parentConversationId: "root",
  depth: 1,
  opened: false,
  ...over,
});

describe("arrange", () => {
  test("puts each child directly beneath its parent, one level down", () => {
    const placed = arrange([
      agent("a"),
      agent("b"),
      agent("a1", { parentConversationId: "c-a" }),
    ]);
    expect(placed.map((p) => [p.subagent.id, p.level])).toEqual([
      ["a", 0],
      ["a1", 1],
      ["b", 0],
    ]);
  });

  test("a card whose parent is not in the list is top level", () => {
    const placed = arrange([agent("x", { parentConversationId: "c-gone" })]);
    expect(placed).toEqual([{ subagent: placed[0]!.subagent, level: 0 }]);
  });

  test("a parent cycle still gives every card one row", () => {
    const placed = arrange([
      agent("p", { parentConversationId: "c-q" }),
      agent("q", { parentConversationId: "c-p" }),
    ]);
    expect(placed.map((p) => p.subagent.id).sort()).toEqual(["p", "q"]);
  });
});

test("byUrgency: blocked, running, failed, cancelled, newest first within a state", () => {
  const sorted = [
    agent("cancelled", { status: "cancelled" }),
    agent("old", { startedAt: "2026-10-16T09:00:00Z" }),
    agent("failed", { status: "failed" }),
    agent("new", { startedAt: "2026-10-16T11:00:00Z" }),
    agent("blocked", { status: "blocked" }),
  ].sort(byUrgency);
  expect(sorted.map((s) => s.id)).toEqual([
    "blocked",
    "new",
    "old",
    "failed",
    "cancelled",
  ]);
});

describe("fullness", () => {
  test("is null until something has measured it", () => {
    expect(fullness({ contextUsed: null, contextWindow: 1000 })).toBeNull();
    expect(fullness({ contextUsed: 10, contextWindow: 0 })).toBeNull();
  });

  test("rounds to a percentage and clamps at 100", () => {
    expect(fullness({ contextUsed: 0, contextWindow: 1000 })).toBe(0);
    expect(fullness({ contextUsed: 425, contextWindow: 1000 })).toBe(43);
    expect(fullness({ contextUsed: 2000, contextWindow: 1000 })).toBe(100);
  });
});

test("gaugeTone turns amber at 75 and red at 90", () => {
  expect(gaugeTone(74)).toBe("default");
  expect(gaugeTone(75)).toBe("warn");
  expect(gaugeTone(89)).toBe("warn");
  expect(gaugeTone(90)).toBe("alert");
});

describe("latestLine", () => {
  test("a failure says why, ahead of any summary", () => {
    expect(
      latestLine({ status: "failed", summary: "half done", error: "boom" }),
    ).toBe("boom");
    expect(latestLine({ status: "failed", summary: null, error: null })).toBe(
      "failed",
    );
  });

  test("the summary when there is one, otherwise what it is doing", () => {
    expect(
      latestLine({ status: "running", summary: "found 3", error: null }),
    ).toBe("found 3");
    expect(latestLine({ status: "blocked", summary: null, error: null })).toBe(
      "working…",
    );
    expect(latestLine({ status: "done", summary: null, error: null })).toBe(
      "reported nothing",
    );
  });
});

describe("countLabel", () => {
  test("lists the non-zero states that need a look", () => {
    const counts = rosterCounts([
      agent("w", { status: "blocked" }),
      agent("r1"),
      agent("r2"),
      agent("f", { status: "failed" }),
      agent("d", { status: "done" }),
    ]);
    expect(countLabel(counts)).toBe("1 waiting · 2 working · 1 failed");
  });

  test("falls back to the finished total, and to nothing for no sub-agents", () => {
    expect(
      countLabel(
        rosterCounts([
          agent("d", { status: "done" }),
          agent("c", { status: "cancelled" }),
        ]),
      ),
    ).toBe("2 done");
    expect(countLabel(rosterCounts([]))).toBe("");
  });
});
