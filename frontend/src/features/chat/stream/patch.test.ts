import { describe, expect, test } from "bun:test";
import type { ChatMessage, HostCommandBlock } from "../model";
import { appendDelta, retractDelta, upsertHost } from "./patch";

/** The terminal on `m`, for reading assertions off. */
function terminal(m: ChatMessage) {
  return (m.blocks ?? []).find(
    (b): b is HostCommandBlock => b.kind === "host_command",
  )?.command;
}

function turn(): ChatMessage {
  return {
    id: "a1",
    role: "assistant",
    content: "",
    blocks: [],
    createdAt: "",
  };
}

describe("a terminal is built up across the events of one call", () => {
  test("the tool that asked is recorded, because more than one can", () => {
    // The conversation grant is keyed by tool name, so a card holding a shell
    // command must not record a decision against the sandboxed host tool.
    const m = turn();
    upsertHost(m, "t1", "shell_run_command", { command: "ls" });
    expect(terminal(m)).toMatchObject({
      name: "shell_run_command",
      command: "ls",
      phase: "pending",
    });
  });

  test("later events fill in the same block", () => {
    const m = turn();
    upsertHost(m, "t1", "shell_run_command", { command: "ls" });
    upsertHost(m, "t1", "shell_run_command", { phase: "ok", stdout: "a\nb\n" });
    expect(m.blocks).toHaveLength(1);
    expect(terminal(m)).toMatchObject({ phase: "ok", stdout: "a\nb\n" });
  });
});

test("a denied terminal stays denied whatever the tool reports next", () => {
  // A denial arrives as a `tool.completed` carrying the refusal the model was
  // handed. For a tool whose ordinary result is also a plain string that would be
  // read as output — repainting a command that never ran as a green OK.
  const m = turn();
  upsertHost(m, "t1", "shell_run_command", { command: "rm -rf /" });
  upsertHost(m, "t1", "shell_run_command", { phase: "denied" });
  upsertHost(m, "t1", "shell_run_command", {
    phase: "ok",
    exitCode: 0,
    stdout: "The operator denied this action.",
  });
  expect(terminal(m)).toMatchObject({ phase: "denied" });
  expect(terminal(m)?.stdout).toBeUndefined();
});

test("a retried request's streamed text is taken back before the retry streams", () => {
  const m = turn();
  appendDelta(m, "text", "Earlier answer. ");
  appendDelta(m, "thinking", "half a thought");
  appendDelta(m, "text", "half an ans");
  retractDelta(m, "text", "half an ans".length);
  retractDelta(m, "thinking", "half a thought".length);
  appendDelta(m, "text", "the full answer");
  expect(m.blocks).toMatchObject([
    { kind: "text", text: "Earlier answer. the full answer" },
  ]);
});
