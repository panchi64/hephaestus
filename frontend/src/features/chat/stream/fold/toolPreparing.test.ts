import { describe, expect, test } from "bun:test";
import { createStore } from "solid-js/store";
import type { RunEvent } from "~/lib/stream";
import { emptyInbox, type InboxState } from "../../inbox";
import type { ChatMessage } from "../../model";
import { createFolder, type FoldState } from "../fold";
import { createPatchById } from "../patch";

function harness(replayThrough = 0) {
  const [messages, setMessages] = createStore<ChatMessage[]>([
    { id: "a1", role: "assistant", content: "", blocks: [], createdAt: "" },
  ]);
  let inbox: InboxState = emptyInbox;
  const state: FoldState = {
    maxFoldedSeq: 0,
    foldTarget: null,
    tasksRevision: 0,
    planRevision: 0,
    activeRunId: "run-1",
    runKind: null,
    epoch: 0,
    replayThrough,
  };
  let opened = 0;
  const fold = createFolder({
    state,
    patchById: createPatchById(messages, setMessages),
    setMessages,
    inbox: {
      get: () => inbox,
      set: (next) => {
        inbox = next;
      },
    },
    setSnapshots: () => {},
    setTasks: () => {},
    setPlan: () => {},
    setPermission: () => {},
    setUsage: () => {},
    setStats: () => {},
    setErrored: () => {},
    setTitlePending: () => {},
    onBrowserOpened: () => {
      opened += 1;
    },
  });
  return {
    fold: (ev: RunEvent) => fold("a1", ev),
    blocks: () => messages[0]!.blocks ?? [],
    opened: () => opened,
  };
}

let seq = 0;
const preparing = (
  id: string,
  chars: number,
  replaces?: string,
  name = "web_search",
): RunEvent => ({
  type: "tool.preparing",
  seq: ++seq,
  ts: "",
  tool_call_id: id,
  name,
  args_chars: chars,
  replaces,
});
const toolStarted = (id: string, name = "web_search"): RunEvent => ({
  type: "tool.started",
  seq: ++seq,
  ts: "",
  tool_call_id: id,
  name,
  args: {},
});
const stepStarted = (): RunEvent => ({
  type: "step.started",
  seq: ++seq,
  ts: "",
  index: 2,
  title: null,
});
const runEnded = (): RunEvent => ({
  type: "run.ended",
  seq: ++seq,
  ts: "",
  outcome: "done",
  detail: null,
});
const browserOpened = (): RunEvent => ({
  type: "browser.opened",
  seq: ++seq,
  ts: "",
});

describe("a call announced before its arguments finish", () => {
  test("preparing makes one block and later frames only move its count", () => {
    const h = harness();
    h.fold(preparing("t1", 10));
    h.fold(preparing("t1", 400));
    expect(h.blocks()).toHaveLength(1);
    const b = h.blocks()[0]!;
    expect(b.kind === "tool" && b.tool.status).toBe("preparing");
    expect(b.kind === "tool" && b.tool.argsChars).toBe(400);
  });

  test("started updates the block in place and clears the count", () => {
    const h = harness();
    h.fold(preparing("t1", 400));
    h.fold(toolStarted("t1"));
    expect(h.blocks()).toHaveLength(1);
    const b = h.blocks()[0]!;
    expect(b.kind === "tool" && b.tool.status).toBe("running");
    expect(b.kind === "tool" && b.tool.argsChars).toBeUndefined();
  });

  test("a terminal started takes the preparing block's position", () => {
    const h = harness();
    h.fold(preparing("t1", 5, undefined, "web_search"));
    h.fold(preparing("sh", 5, undefined, "shell_run_command"));
    h.fold(preparing("t2", 5, undefined, "web_fetch"));
    h.fold(toolStarted("sh", "shell_run_command"));
    expect(h.blocks().map((b) => b.id)).toEqual([
      "tool-t1",
      "host-sh",
      "tool-t2",
    ]);
  });

  test("replaces re-keys the announced block instead of adding a card", () => {
    const h = harness();
    h.fold(preparing("old", 5));
    h.fold(preparing("new", 30, "old"));
    expect(h.blocks()).toHaveLength(1);
    const b = h.blocks()[0]!;
    expect(b.id).toBe("tool-new");
    expect(b.kind === "tool" && b.tool.id).toBe("new");
    expect(b.kind === "tool" && b.tool.argsChars).toBe(30);
  });

  test("a step or run boundary drops calls that never started", () => {
    const h = harness();
    h.fold(preparing("t1", 5));
    h.fold(stepStarted());
    expect(h.blocks()).toHaveLength(0);
    h.fold(preparing("t2", 5));
    h.fold(runEnded());
    expect(h.blocks()).toHaveLength(0);
  });
});

describe("browser.opened", () => {
  test("announces live and stays quiet on replay", () => {
    const live = harness();
    live.fold(browserOpened());
    expect(live.opened()).toBe(1);

    const replayed = harness(Number.MAX_SAFE_INTEGER);
    replayed.fold(browserOpened());
    expect(replayed.opened()).toBe(0);
  });
});
