import { describe, expect, test } from "bun:test";
import { createStore } from "solid-js/store";
import type { RunEvent } from "~/lib/stream";
import { toMessage } from "../data/messages";
import type { ChatMessage, ToolBlock, ToolInvocation } from "../model";
import { createFolder, type FoldState } from "./fold";
import { createPatchById } from "./patch";

/**
 * Where a script's own tool calls land.
 *
 * A `run_code` call's script makes calls of its own, and each streams as an ordinary
 * tool frame naming the script's call as its parent. What is pinned here is that such a
 * call nests on the script's card and never becomes a step on the rail — live, when its
 * frames arrive out of order, and on a reload, which has to put it in the same place.
 */

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
  });
  return {
    fold: (ev: RunEvent) => fold("a1", ev),
    /** The tool calls on the turn's rail, in order. */
    rail: (): ToolInvocation[] =>
      (messages[1].blocks ?? [])
        .filter((b): b is ToolBlock => b.kind === "tool")
        .map((b) => b.tool),
  };
}

let seq = 0;
const base = () => ({ seq: ++seq, ts: "", conversation_id: "c1" });

const SCRIPT = "a = await files_read_file(path='x')\nprint(a)";

const started = (
  id: string,
  name: string,
  args: Record<string, unknown>,
  parent?: string,
): RunEvent => ({
  ...base(),
  type: "tool.started",
  tool_call_id: id,
  name,
  args,
  parent_tool_call_id: parent ?? null,
});

const completed = (
  id: string,
  name: string,
  result: unknown,
  parent?: string,
): RunEvent => ({
  ...base(),
  type: "tool.completed",
  tool_call_id: id,
  name,
  result,
  parent_tool_call_id: parent ?? null,
});

describe("a script's calls nest on its card", () => {
  test("a call with a parent joins the parent's children, not the rail", () => {
    const h = harness();
    h.fold(started("rc1", "run_code", { code: SCRIPT }));
    h.fold(started("n1", "files_read_file", { path: "x" }, "rc1"));
    const rail = h.rail();
    expect(rail.map((t) => t.id)).toEqual(["rc1"]);
    expect(rail[0].children?.map((c) => c.id)).toEqual(["n1"]);
    expect(rail[0].children?.[0].status).toBe("running");
  });

  test("the script shows as code, not as an argument", () => {
    const h = harness();
    h.fold(started("rc1", "run_code", { code: SCRIPT, restart: true }));
    const [script] = h.rail();
    expect(script.script).toBe(SCRIPT);
    expect(script.args).toBe("restart=true");
  });

  test("a nested call settles on its own row", () => {
    const h = harness();
    h.fold(started("rc1", "run_code", { code: SCRIPT }));
    h.fold(started("n1", "files_read_file", { path: "x" }, "rc1"));
    h.fold(started("n2", "files_read_file", { path: "y" }, "rc1"));
    h.fold(completed("n1", "files_read_file", "hello", "rc1"));
    h.fold({
      ...base(),
      type: "tool.failed",
      tool_call_id: "n2",
      name: "files_read_file",
      error: "no such file",
      parent_tool_call_id: "rc1",
    });
    const [script] = h.rail();
    const [n1, n2] = script.children ?? [];
    expect(n1).toMatchObject({ status: "ok", result: "hello" });
    expect(n2).toMatchObject({ status: "error", error: "no such file" });
    // The script is still out: its own completion is its own frame.
    expect(script.status).toBe("running");
  });

  test("progress finds a nested call by id alone", () => {
    const h = harness();
    h.fold(started("rc1", "run_code", { code: SCRIPT }));
    h.fold(started("n1", "files_read_file", { path: "x" }, "rc1"));
    h.fold({
      ...base(),
      type: "tool.progress",
      tool_call_id: "n1",
      elapsed_s: 1.5,
      partial: "reading",
    });
    expect(h.rail()[0].children?.[0]).toMatchObject({
      progress: "reading",
      elapsedMs: 1500,
    });
  });

  test("a terminal tool made from a script is a nested row, not a terminal", () => {
    const h = harness();
    h.fold(started("rc1", "run_code", { code: SCRIPT }));
    h.fold(started("n1", "shell_run_command", { command: "ls" }, "rc1"));
    h.fold(completed("n1", "shell_run_command", { exit_code: 0 }, "rc1"));
    const turn = h.rail();
    expect(turn[0].children?.[0]).toMatchObject({
      name: "shell_run_command",
      status: "ok",
      outcome: "exit 0",
    });
  });

  test("a child that arrives before its parent still nests", () => {
    const h = harness();
    h.fold(started("n1", "files_read_file", { path: "x" }, "rc1"));
    expect(h.rail().map((t) => t.id)).toEqual(["rc1"]);
    h.fold(started("rc1", "run_code", { code: SCRIPT }));
    const rail = h.rail();
    // One row for the script, filled in by its own frame, keeping what it collected.
    expect(rail.map((t) => t.id)).toEqual(["rc1"]);
    expect(rail[0].script).toBe(SCRIPT);
    expect(rail[0].children?.map((c) => c.id)).toEqual(["n1"]);
  });
});

describe("a reload nests the same way", () => {
  test("persisted rows with a parent land on the script's card", () => {
    const m = toMessage({
      id: "m1",
      role: "assistant",
      content: "",
      tools: [
        {
          id: "rc1",
          name: "run_code",
          args: { code: SCRIPT },
          status: "ok",
          result: "done",
        },
        {
          id: "n1",
          name: "files_read_file",
          args: { path: "x" },
          status: "ok",
          result: "hello",
          parent_tool_call_id: "rc1",
        },
        {
          id: "n2",
          name: "shell_run_command",
          args: { command: "ls" },
          status: "error",
          error: "denied",
          parent_tool_call_id: "rc1",
        },
      ],
    });
    const tools = (m.blocks ?? []).filter(
      (b): b is ToolBlock => b.kind === "tool",
    );
    expect(m.blocks?.some((b) => b.kind === "host_command")).toBe(false);
    expect(tools.map((b) => b.tool.id)).toEqual(["rc1"]);
    expect(tools[0].tool.script).toBe(SCRIPT);
    expect(tools[0].tool.children).toMatchObject([
      { id: "n1", status: "ok", result: "hello" },
      { id: "n2", status: "error", error: "denied" },
    ]);
  });

  test("a nested row listed before its script still nests", () => {
    const m = toMessage({
      id: "m1",
      role: "assistant",
      content: "",
      tools: [
        {
          id: "n1",
          name: "files_read_file",
          args: { path: "x" },
          status: "ok",
          parent_tool_call_id: "rc1",
        },
        { id: "rc1", name: "run_code", args: { code: SCRIPT }, status: "ok" },
      ],
    });
    const tools = (m.blocks ?? []).filter(
      (b): b is ToolBlock => b.kind === "tool",
    );
    expect(tools.map((b) => b.id)).toEqual(["m1-rc1"]);
    expect(tools[0].tool).toMatchObject({ status: "ok", script: SCRIPT });
    expect(tools[0].tool.children?.map((c) => c.id)).toEqual(["n1"]);
  });
});
