import { describe, expect, test } from "bun:test";
import { createStore } from "solid-js/store";
import type { RunEvent } from "~/lib/stream";
import { emptyInbox, type InboxState } from "../../inbox";
import type { ChatMessage } from "../../model";
import { createFolder, type FoldState } from "../fold";
import { createPatchById } from "../patch";

function harness() {
  const [messages, setMessages] = createStore<ChatMessage[]>([
    { id: "u1", role: "user", content: "go on", createdAt: "" },
    { id: "a1", role: "assistant", content: "", blocks: [], createdAt: "" },
  ]);
  const state: FoldState = {
    maxFoldedSeq: 0,
    foldTarget: null,
    tasksRevision: 0,
    planRevision: 0,
    activeRunId: "run-1",
    runKind: null,
    epoch: 0,
    replayThrough: 0,
  };
  let inbox: InboxState = emptyInbox;
  const fold = createFolder({
    state,
    patchById: createPatchById(messages, setMessages),
    setMessages,
    setSnapshots: () => {},
    setTasks: () => {},
    setPlan: () => {},
    setPermission: () => {},
    setUsage: () => {},
    setStats: () => {},
    setErrored: () => {},
    setTitlePending: () => {},
    inbox: { get: () => inbox, set: (next) => (inbox = next) },
  });
  return { fold: (ev: RunEvent) => fold("a1", ev), messages };
}

describe("a chassis reminder in the live fold", () => {
  test("an injected system item becomes a notice row, not the operator's turn", () => {
    const h = harness();
    h.fold({
      type: "message.queued",
      seq: 1,
      ts: "",
      message_id: "q1",
      text: "- write the test\n- fix the bug",
      source: "system",
      notice: "open-tasks",
    });
    expect(h.messages.some((m) => m.role === "notice")).toBe(false);
    h.fold({
      type: "message.injected",
      seq: 2,
      ts: "",
      message_id: "q1",
      source: "system",
    });
    const notice = h.messages.find((m) => m.role === "notice");
    expect(notice?.notice).toBe("open-tasks");
    expect(notice?.content).toBe("- write the test\n- fix the bug");
    expect(h.messages.filter((m) => m.role === "user")).toHaveLength(1);
  });
});
