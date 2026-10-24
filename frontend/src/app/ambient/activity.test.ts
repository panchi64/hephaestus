import { describe, expect, test } from "bun:test";
import type { ChatSummary } from "~/features/chat/model";
import { activityLoad, diffActivity, liveSubagents } from "./activity";

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

  test("leaves spawned threads to the aurora", () => {
    expect(
      activityLoad([
        row("a", { activity: "running" }),
        row("child", { activity: "running", parentId: "a" }),
      ]),
    ).toBe(1);
  });
});

describe("liveSubagents", () => {
  test("counts only spawned threads with a live run", () => {
    expect(
      liveSubagents([
        row("a", { activity: "running" }),
        row("b", { activity: "running", parentId: "a" }),
        row("c", { activity: "awaiting_input", parentId: "a" }),
        row("d", { parentId: "a" }),
      ]),
    ).toBe(2);
  });
});

describe("diffActivity", () => {
  test("the first read fires nothing", () => {
    const { strikes } = diffActivity(undefined, [
      row("a", { activity: "running" }),
    ]);
    expect(strikes).toEqual([]);
  });

  test("each transition strikes in its tone", () => {
    const { snapshot } = diffActivity(undefined, [
      row("start"),
      row("ask", { activity: "running" }),
      row("ok", { activity: "running" }),
      row("fail", { activity: "running" }),
    ]);
    const { strikes } = diffActivity(snapshot, [
      row("start", { activity: "running" }),
      row("ask", { activity: "awaiting_input" }),
      row("ok", { lastOutcome: "done" }),
      row("fail", { lastOutcome: "error" }),
      row("new", { activity: "queued" }),
    ]);
    expect(strikes).toEqual([
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
    const { strikes } = diffActivity(snapshot, [
      row("a", { activity: "running" }),
      row("b", { activity: "running" }),
    ]);
    expect(strikes).toEqual([]);
  });

  test("spawned threads never strike", () => {
    const { snapshot } = diffActivity(undefined, [
      row("child", { parentId: "a" }),
    ]);
    const { strikes } = diffActivity(snapshot, [
      row("child", { parentId: "a", activity: "running" }),
    ]);
    expect(strikes).toEqual([]);
  });
});
