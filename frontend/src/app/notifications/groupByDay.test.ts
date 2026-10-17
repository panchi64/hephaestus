import { describe, expect, test } from "bun:test";
import type { Notification } from "~/lib/stream/notificationEvents";
import { groupByDay } from "./groupByDay";

const now = new Date(2026, 9, 17, 15, 0, 0);

function at(day: number, hour: number): Notification {
  return {
    id: `${day}-${hour}`,
    kind: "system",
    title: "t",
    body: null,
    conversationId: null,
    runId: null,
    createdAt: new Date(2026, 9, day, hour).toISOString(),
    readAt: null,
    resolvedAt: null,
  };
}

describe("groupByDay", () => {
  test("splits into Today, Yesterday and Earlier by local day", () => {
    const groups = groupByDay(
      [at(17, 9), at(16, 23), at(16, 0), at(15, 12), at(1, 1)],
      now,
    );
    expect(groups.map((g) => g.label)).toEqual([
      "Today",
      "Yesterday",
      "Earlier",
    ]);
    expect(groups[0].items.map((n) => n.id)).toEqual(["17-9"]);
    expect(groups[1].items.map((n) => n.id)).toEqual(["16-23", "16-0"]);
    expect(groups[2].items.map((n) => n.id)).toEqual(["15-12", "1-1"]);
  });

  test("omits empty buckets", () => {
    const groups = groupByDay([at(10, 1)], now);
    expect(groups.map((g) => g.label)).toEqual(["Earlier"]);
  });

  test("empty input has no groups", () => {
    expect(groupByDay([], now)).toEqual([]);
  });
});
