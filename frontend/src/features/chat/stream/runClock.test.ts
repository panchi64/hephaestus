import { describe, expect, test } from "bun:test";
import { createStore } from "solid-js/store";
import type { RunEvent } from "~/lib/stream";
import type { ChatMessage } from "../model";
import {
  createFolder,
  type FoldState,
  type RunClock,
  type RunQueue,
} from "./fold";
import { createPatchById } from "./patch";

/**
 * The composer header's run clock: the backend's start instant and step, lifted off the
 * run's own events. What is pinned is that nothing is invented — the instant is
 * `run.started`'s `ts` verbatim, a step with no start to pair it with stays unset, and
 * the clock goes when the run does.
 */

function harness() {
  const [messages, setMessages] = createStore<ChatMessage[]>([
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
  let clock: RunClock | null = null;
  let queue: RunQueue | null = null;
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
    setRunClock: (next) => {
      clock = next(clock);
    },
    setRunQueue: (next) => {
      queue = next;
    },
  });
  let seq = 0;
  return {
    fold: (ev: Record<string, unknown>) =>
      fold("a1", { seq: ++seq, ts: "", ...ev } as RunEvent),
    clock: () => clock,
    queue: () => queue,
  };
}

const START = "2026-09-26T10:00:00Z";

describe("run clock", () => {
  test("starts at run.started's own instant, with no step yet", () => {
    const h = harness();
    h.fold({
      type: "run.started",
      ts: START,
      run_id: "run-1",
      kind: "chat",
      protocol_version: 1,
    });
    expect(h.clock()).toEqual({ startedAt: START, step: null });
  });

  test("takes the step the backend numbered, not a count of events", () => {
    const h = harness();
    h.fold({
      type: "run.started",
      ts: START,
      run_id: "run-1",
      kind: "chat",
      protocol_version: 1,
    });
    // Out of order on purpose: a replay folding step 3 must not read as step 1.
    h.fold({ type: "step.started", index: 3, title: null });
    expect(h.clock()?.step).toBe(3);
    h.fold({ type: "step.started", index: 4, title: null });
    expect(h.clock()).toEqual({ startedAt: START, step: 4 });
  });

  test("a step with no run.started before it is not a clock", () => {
    const h = harness();
    h.fold({ type: "step.started", index: 2, title: null });
    expect(h.clock()).toBeNull();
  });

  test("clears when the run ends", () => {
    const h = harness();
    h.fold({
      type: "run.started",
      ts: START,
      run_id: "run-1",
      kind: "chat",
      protocol_version: 1,
    });
    h.fold({ type: "run.ended", outcome: "done", detail: null });
    expect(h.clock()).toBeNull();
  });
});

describe("run queue", () => {
  const started = {
    type: "run.started",
    ts: START,
    run_id: "run-1",
    kind: "chat",
    protocol_version: 1,
  };

  test("is set by run.queued and moved by the next one", () => {
    const h = harness();
    h.fold({ type: "run.queued", position: 2, endpoint: "local llama" });
    expect(h.queue()).toEqual({ endpoint: "local llama", position: 2 });
    h.fold({ type: "run.queued", position: 1, endpoint: "local llama" });
    expect(h.queue()).toEqual({ endpoint: "local llama", position: 1 });
  });

  test("clears when the run starts", () => {
    const h = harness();
    h.fold({ type: "run.queued", position: 1, endpoint: "local llama" });
    h.fold(started);
    expect(h.queue()).toBeNull();
  });

  test("clears on a terminal event", () => {
    for (const end of [
      { type: "run.ended", outcome: "cancelled", detail: null },
      { type: "run.error", message: "gone", kind: null },
    ]) {
      const h = harness();
      h.fold({ type: "run.queued", position: 3, endpoint: "local llama" });
      h.fold(end);
      expect(h.queue()).toBeNull();
    }
  });
});
