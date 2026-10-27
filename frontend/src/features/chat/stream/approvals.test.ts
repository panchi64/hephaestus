import { describe, expect, test } from "bun:test";
import { createRoot, createSignal } from "solid-js";
import { createStore } from "solid-js/store";
import type { AssistantBlock, ChatMessage } from "../model";
import { createApprovalOps } from "./approvals";

/**
 * Whether the room is waiting on the operator — read off the blocks rather than tracked
 * beside them, which is the point of the memo and the thing worth pinning.
 *
 * It drives the nav rail's warn tone and the favicon tint from any screen, so both
 * directions are load-bearing: missing a park leaves a run stalled with nothing on
 * screen saying so, and a flag that fails to clear leaves the whole shell claiming a
 * decision is owed forever. The subtle half is *which* turn it reads: a park is by
 * definition the live turn waiting, so a pending-looking block on an older turn — a
 * card left stale by a decision made in another tab — must not answer for it.
 */

function ask(over: Partial<ChatMessage> = {}, ...blocks: AssistantBlock[]) {
  return {
    id: "a1",
    role: "assistant",
    content: "",
    blocks,
    createdAt: "",
    ...over,
  } as ChatMessage;
}

const approval = (stale = false): AssistantBlock => ({
  kind: "approval",
  id: "ap1",
  approval: {
    toolCallId: "t1",
    name: "shell_run_command",
    args: {},
    summary: "Runs ls",
    stale,
  },
});

const host = (phase: "pending" | "running" | "stale"): AssistantBlock => ({
  kind: "host_command",
  id: "hc1",
  command: { toolCallId: "t2", name: "run_host_command", command: "ls", phase },
});

function waiting(messages: ChatMessage[], sending = true): boolean {
  return createRoot((dispose) => {
    const [store] = createStore<ChatMessage[]>(messages);
    const [live] = createSignal(sending);
    const ops = createApprovalOps({
      messages: store,
      patchById: () => {},
      sending: live,
      activeRunId: () => "run-1",
      reconcileStaleDecision: async () => {},
    });
    const answer = ops.awaitingKind() !== null;
    dispose();
    return answer;
  });
}

describe("a live turn parked on a decision", () => {
  test("an undecided approval card is a park", () => {
    expect(waiting([ask({ streaming: true }, approval())])).toBe(true);
  });

  test("so is a host command still asking", () => {
    expect(waiting([ask({ streaming: true }, host("pending"))])).toBe(true);
  });

  test("a detached turn still counts — the run may be parked server-side", () => {
    expect(waiting([ask({ detached: true }, approval())])).toBe(true);
  });
});

describe("what is not a park", () => {
  test("no run in flight, whatever the transcript still shows", () => {
    // The run ended: the cards are history, and the shell must stop flagging them.
    expect(waiting([ask({ streaming: true }, approval())], false)).toBe(false);
  });

  test("a card left stale by a decision made somewhere else", () => {
    expect(waiting([ask({ streaming: true }, approval(true))])).toBe(false);
  });

  test("a host command that is already running", () => {
    expect(waiting([ask({ streaming: true }, host("running"))])).toBe(false);
  });

  test("a pending block on an earlier turn, while the live one asks nothing", () => {
    expect(
      waiting([
        ask({ id: "a0" }, approval()),
        ask({ id: "a1", streaming: true }),
      ]),
    ).toBe(false);
  });

  test("a turn with no blocks at all", () => {
    expect(waiting([ask({ streaming: true })])).toBe(false);
  });
});
