import { describe, expect, test } from "bun:test";
import type { ChatSummary } from "~/features/chat/model";
import { activityLoad, diffActivity } from "./activity";

const row = (id: string, over: Partial<ChatSummary> = {}): ChatSummary => ({
  id,
  title: id,
  updatedAt: "2026-10-20T00:00:00Z",
  createdAt: "2026-10-20T00:00:00Z",
  messageCount: 1,
  ...over,
});

describe("activityLoad", () => {
  test("counts live threads, and a wait on the operator twice", () => {
    expect(
      activityLoad([
        row("a", { activity: "running" }),
        row("b", { activity: "queued" }),
        row("c", { activity: "awaiting_input" }),
        row("d"),
      ]),
    ).toBe(4);
  });
});

describe("diffActivity", () => {
  test("the first read fires nothing", () => {
    const { pulses } = diffActivity(undefined, [
      row("a", { activity: "running" }),
    ]);
    expect(pulses).toEqual([]);
  });

  test("each transition glows in its tone", () => {
    const { snapshot } = diffActivity(undefined, [
      row("start"),
      row("ask", { activity: "running" }),
      row("ok", { activity: "running" }),
      row("fail", { activity: "running" }),
    ]);
    const { pulses } = diffActivity(snapshot, [
      row("start", { activity: "running" }),
      row("ask", { activity: "awaiting_input" }),
      row("ok", { lastOutcome: "done" }),
      row("fail", { lastOutcome: "error" }),
      row("new", { activity: "queued" }),
    ]);
    expect(pulses).toEqual([
      { threadId: "start", tone: "info" },
      { threadId: "ask", tone: "warn" },
      { threadId: "ok", tone: "nominal" },
      { threadId: "fail", tone: "alert" },
      { threadId: "new", tone: "info" },
    ]);
  });

  test("an unchanged row fires nothing, and neither does a resumed one", () => {
    const { snapshot } = diffActivity(undefined, [
      row("a", { activity: "running" }),
      row("b", { activity: "awaiting_input" }),
    ]);
    const { pulses } = diffActivity(snapshot, [
      row("a", { activity: "running" }),
      row("b", { activity: "running" }),
    ]);
    expect(pulses).toEqual([]);
  });
});
