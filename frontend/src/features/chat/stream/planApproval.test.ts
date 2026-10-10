/** A submitted plan, on its way to and from the operator.
 *
 *  Two seams, and both are easy to get wrong in a way nothing visible catches.
 *
 *  **The mark.** A plan is decided in the dock with every other approval, and is only
 *  *marked* on the park so the dock can offer Request changes over it and the panel can
 *  be pointed at. The mark must never cost it its place in the batch — the run resumes on
 *  one body covering every parked call.
 *
 *  **The third answer.** "Revise" and "deny" both stop the call and differ only in what
 *  the model is told. A model handed the wrong one wastes the turn: a denial read as
 *  "try again" produces a second attempt at something already refused, and a revision
 *  request read as a flat no produces an apology and a stop.
 */

import { describe, expect, test } from "bun:test";
import { createFolder, type FoldState } from "./fold";
import { createApprovalOps, PLAN_SUBMIT_TOOL } from "./approvals";
import type { PermissionLevel, PlanDocument } from "../model";
import type { RunEvent } from "~/lib/stream";

function foldState(): FoldState {
  return {
    maxFoldedSeq: 0,
    foldTarget: null,
    tasksRevision: 0,
    planRevision: 0,
    activeRunId: "run-1",
    runKind: null,
    epoch: 0,
    replayThrough: 0,
  };
}

/** The fold, with only the conversation-scoped writes this file cares about captured. */
function harness() {
  const state = foldState();
  let plan: PlanDocument | null = null;
  let level: PermissionLevel | null = null;
  let tasks: unknown[] = [];
  const fold = createFolder({
    state,
    patchById: (_id, _mutate) => {},
    setMessages: (() => {}) as never,
    setSnapshots: () => {},
    setTasks: (items) => {
      tasks = items;
    },
    setPlan: (next) => {
      plan = next;
    },
    setPermission: (next) => {
      level = next;
    },
    setUsage: () => {},
    setStats: () => {},
    setErrored: () => {},
    setTitlePending: () => {},
  });
  return {
    fold: (ev: Record<string, unknown>, seq = 1) =>
      fold("a1", { seq, ...ev } as unknown as RunEvent),
    state,
    plan: () => plan,
    level: () => level,
    tasks: () => tasks,
  };
}

const planEvent = (over: Record<string, unknown> = {}) => ({
  type: "plan.updated",
  title: "Rewrite the parser",
  body: "## Why\nIt is slow.",
  steps: ["read it", "change it"],
  status: "pending",
  revision: 1,
  ...over,
});

describe("folding the plan document", () => {
  test("it arrives whole, not as a delta", () => {
    const h = harness();
    h.fold(planEvent());
    expect(h.plan()).toEqual({
      title: "Rewrite the parser",
      body: "## Why\nIt is slow.",
      steps: ["read it", "change it"],
      status: "pending",
      revision: 1,
    });
  });

  test("its counter is its own, so a task update does not look like one", () => {
    // Each backfill drops its answer if *its* surface moved while the fetch was in
    // flight. One shared counter would make the task backfill think the stream had
    // overtaken it every time a plan arrived, and vice versa.
    const h = harness();
    h.fold({ type: "tasks.updated", items: [] }, 1);
    expect(h.state.tasksRevision).toBe(1);
    expect(h.state.planRevision).toBe(0);
    h.fold(planEvent(), 2);
    expect(h.state.planRevision).toBe(1);
    expect(h.state.tasksRevision).toBe(1);
  });

  test("a revision replaces the plan rather than merging into it", () => {
    const h = harness();
    h.fold(planEvent(), 1);
    h.fold(planEvent({ body: "answering the feedback", revision: 2 }), 2);
    expect(h.plan()?.body).toBe("answering the feedback");
    expect(h.plan()?.revision).toBe(2);
  });

  test("an approved plan seeds the task list through the ordinary event", () => {
    // The backend seeds it and announces on `tasks.updated` like any other write, so
    // the panel cannot tell a seeded list from one the model typed.
    const h = harness();
    h.fold(planEvent({ status: "approved", revision: 1 }), 1);
    h.fold(
      {
        type: "tasks.updated",
        items: [{ id: "t1", content: "read it", status: "pending" }],
      },
      2,
    );
    expect(h.plan()?.status).toBe("approved");
    expect(h.tasks()).toHaveLength(1);
  });
});

describe("folding a level the run moved", () => {
  test("it re-seats the level rather than only reporting it", () => {
    // The client sends the level back on every message. Without this the next send
    // would write the stale one straight over the change.
    const h = harness();
    h.fold(
      { type: "permission.changed", level: "plan", reason: "entering" },
      1,
    );
    expect(h.level()).toBe("plan");
    h.fold(
      { type: "permission.changed", level: "auto", reason: "plan approved" },
      2,
    );
    expect(h.level()).toBe("auto");
  });

  test("a level this build has no rule for degrades to the strictest", () => {
    // It matters *because* the level is sent back: a raw cast would seat the unknown
    // string, ride it on the next message and have it refused at the edge — the
    // operator's message failing for a reason nothing on screen explains. The strictest
    // reading is the only one that cannot widen a thread.
    const h = harness();
    h.fold({ type: "permission.changed", level: "root", reason: "?" }, 1);
    expect(h.level()).toBe("plan");
  });
});

describe("the tool name the park marks on", () => {
  test("matches the one the backend defers", () => {
    // Both halves are literals — the frontend cannot import the backend's — so this
    // is the only thing standing between a rename and a plan that silently renders as
    // an ordinary approval in the dock.
    expect(PLAN_SUBMIT_TOOL).toBe("plan_submit");
  });
});

describe("marking the plan in the park", () => {
  const approval = (name: string) => ({
    kind: "approval" as const,
    approval: { toolCallId: `c-${name}`, name, args: {}, summary: name },
  });
  const question = () => ({
    kind: "question" as const,
    question: { toolCallId: "q1", questions: [] },
  });

  /** The park memo, over a single live message carrying `blocks`. */
  const parkOf = (blocks: unknown[]) => {
    const messages = [{ id: "m1", streaming: true, blocks }] as never;
    return createApprovalOps({
      messages,
      patchById: () => {},
      sending: () => true,
      activeRunId: () => "run-1",
      reconcileStaleDecision: async () => {},
    }).park();
  };

  test("a plan is marked AND stays in the batch the dock decides", () => {
    // The run resumes on ONE body covering every parked call. The plan used to be
    // lifted out of `approvals` for a panel that answered it alone, which made the mark
    // and the batch two different facts; now the mark is only a mark, and a park that
    // held nothing else would otherwise reach the dock empty.
    const park = parkOf([approval(PLAN_SUBMIT_TOOL)]);
    expect(park?.planApproval?.name).toBe(PLAN_SUBMIT_TOOL);
    expect(park?.approvals.map((a) => a.name)).toEqual([PLAN_SUBMIT_TOOL]);
  });

  test("a plan with anything beside it is still one batch", () => {
    for (const extra of [approval("shell_run_command"), question()]) {
      const park = parkOf([approval(PLAN_SUBMIT_TOOL), extra]);
      expect(park?.planApproval?.name).toBe(PLAN_SUBMIT_TOOL);
      expect(park?.approvals.map((a) => a.name)).toContain(PLAN_SUBMIT_TOOL);
    }
  });

  test("an ordinary approval is never mistaken for a plan", () => {
    const park = parkOf([approval("shell_run_command")]);
    expect(park?.planApproval).toBeNull();
    expect(park?.approvals).toHaveLength(1);
  });
});
