import { describe, expect, it } from "bun:test";
import type { ChatSummary } from "~/features/chat/model";
import type { ActiveRun, CapabilityHealth } from "./model";
import {
  FAILURE_WINDOW_MS,
  buildActivity,
  degraded,
  homeDigest,
} from "./activity";

const NOW = Date.parse("2026-10-18T13:00:00Z");

function thread(over: Partial<ChatSummary> & { id: string }): ChatSummary {
  return {
    title: over.id,
    updatedAt: "2026-10-18T10:00:00Z",
    createdAt: "2026-10-18T09:00:00Z",
    messageCount: 2,
    ...over,
  };
}

function run(over: Partial<ActiveRun> & { id: string }): ActiveRun {
  return {
    kind: "CHAT",
    label: over.id,
    status: "running",
    detail: "",
    ...over,
  };
}

function cap(
  over: Partial<CapabilityHealth> & { key: string },
): CapabilityHealth {
  return {
    label: over.key,
    status: "nominal",
    detail: "",
    critical: false,
    ...over,
  };
}

describe("buildActivity", () => {
  it("puts waiting, then running, then queued work ahead of recent threads", () => {
    const { items } = buildActivity(
      [
        thread({ id: "recent", updatedAt: "2026-10-18T12:00:00Z" }),
        thread({ id: "queued", activity: "queued" }),
        thread({ id: "running", activity: "running" }),
        thread({ id: "older", updatedAt: "2026-10-17T12:00:00Z" }),
        thread({ id: "waiting", activity: "awaiting_input" }),
      ],
      [],
      10,
      NOW,
    );
    expect(items.map((i) => i.key)).toEqual([
      "waiting",
      "running",
      "queued",
      "recent",
      "older",
    ]);
  });

  it("takes a live thread's clock from its run and keeps the outcome at rest", () => {
    const { items } = buildActivity(
      [
        thread({ id: "a", activity: "running" }),
        thread({ id: "b", lastOutcome: "error" }),
      ],
      [
        run({
          id: "r1",
          conversationId: "a",
          startedAt: "2026-10-18T11:00:00Z",
          detail: "step 3",
        }),
      ],
      10,
      NOW,
    );
    expect(items[0]!.startedAt).toBe("2026-10-18T11:00:00Z");
    expect(items[0]!.detail).toBe("step 3");
    expect(items[1]!.state).toBe("error");
    expect(items[1]!.startedAt).toBeUndefined();
  });

  it("lists a stateless run as its own row with no thread", () => {
    const { items, total } = buildActivity(
      [thread({ id: "a" })],
      [
        run({
          id: "r2",
          label: "Nightly backfill",
          startedAt: "2026-10-18T11:00:00Z",
        }),
      ],
      10,
      NOW,
    );
    expect(total).toBe(2);
    expect(items[0]).toMatchObject({
      key: "run:r2",
      title: "Nightly backfill",
      state: "running",
    });
    expect(items[0]!.conversationId).toBeUndefined();
  });

  it("limits the rows and reports the total", () => {
    const sessions = Array.from({ length: 12 }, (_, i) =>
      thread({ id: `t${i}` }),
    );
    const { items, total } = buildActivity(sessions, [], 8, NOW);
    expect(items).toHaveLength(8);
    expect(total).toBe(12);
  });

  it("orders by instant, not by how the timestamp is written", () => {
    const { items } = buildActivity(
      [
        thread({ id: "zoned", updatedAt: "2026-10-18T10:00:00Z" }),
        thread({ id: "fractional", updatedAt: "2026-10-18T10:00:00.500Z" }),
      ],
      [],
      10,
      NOW,
    );
    expect(items.map((i) => i.key)).toEqual(["fractional", "zoned"]);
  });

  it("counts every row, but not the hidden threads of sub-agent runs", () => {
    const sessions = [
      ...Array.from({ length: 10 }, (_, i) => thread({ id: `t${i}` })),
      thread({ id: "live", activity: "running" }),
      thread({ id: "ask", activity: "awaiting_input" }),
    ];
    const { counts } = buildActivity(
      sessions,
      [
        run({ id: "r1", conversationId: "live" }),
        run({ id: "r2", conversationId: "hidden-sub-agent" }),
        run({ id: "r3", label: "Nightly backfill", status: "queued" }),
      ],
      3,
      NOW,
    );
    expect(counts).toEqual({ running: 2, waiting: 1, failed: 0 });
  });

  it("counts a failure only within the window", () => {
    const recent = new Date(NOW - 1000).toISOString();
    const stale = new Date(NOW - FAILURE_WINDOW_MS - 1000).toISOString();
    const { counts } = buildActivity(
      [
        thread({ id: "new", lastOutcome: "error", updatedAt: recent }),
        thread({ id: "old", lastOutcome: "error", updatedAt: stale }),
      ],
      [],
      10,
      NOW,
    );
    expect(counts.failed).toBe(1);
  });
});

describe("degraded", () => {
  it("keeps only warn and alert capabilities, alerts first", () => {
    const got = degraded([
      cap({ key: "a", status: "warn" }),
      cap({ key: "b" }),
      cap({ key: "c", status: "alert" }),
    ]);
    expect(got.map((c) => c.key)).toEqual(["c", "a"]);
  });
});

describe("homeDigest", () => {
  const none = { running: 0, waiting: 0, failed: 0, critical: [] };

  it("says when nothing is running", () => {
    expect(homeDigest(none)).toBe("Nothing is running.");
  });

  it("counts runs and asks in words", () => {
    expect(homeDigest({ ...none, running: 2, waiting: 1 })).toBe(
      "Two runs going. One is waiting on you.",
    );
    expect(homeDigest({ ...none, running: 1 })).toBe("One run going.");
    expect(homeDigest({ ...none, waiting: 2 })).toBe(
      "Two threads are waiting on you.",
    );
  });

  it("names failures and critical capabilities", () => {
    expect(
      homeDigest({
        ...none,
        failed: 1,
        critical: [
          { label: "Chat model", status: "alert" },
          { label: "Embeddings", status: "warn" },
        ],
      }),
    ).toBe(
      "Nothing is running. One thread ended in an error in the last day. Chat model is unavailable. Embeddings is degraded.",
    );
  });

  it("falls back to digits past nine", () => {
    expect(homeDigest({ ...none, running: 12 })).toBe("12 runs going.");
  });
});
