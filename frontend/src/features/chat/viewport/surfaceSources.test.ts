/** What each surface has, and whether it may interrupt.
 *
 *  Availability is the cheap half and still worth pinning: it decides which header
 *  buttons exist, so a source that answers wrongly either hides a surface the operator
 *  has content for or offers one that opens onto nothing.
 *
 *  **Arrival is the half with a judgement in it.** The rule is that a plan puts itself on
 *  screen while it is waiting on the operator's answer and otherwise only badges — which
 *  depends on the moment rather than on what kind of surface a plan is. That is the whole
 *  reason arrival is a predicate, and these are the cases that would make a static field
 *  wrong.
 *
 *  It used to be inferred from the thread's permission level, which needed a guard
 *  against the not-yet-loaded stand-in (the stand-in *is* `plan`). The plan now carries
 *  its own status, so the inference and its guard are both gone — and the tests for them
 *  with it.
 */

import { describe, expect, test } from "bun:test";
import {
  arrivalClaims,
  createSurfaceSources,
  type SurfaceDeps,
} from "./surfaceSources";
import type { HostCommand, PlanDocument, PlanStatus } from "../model";
import type { TaskItem } from "~/lib/stream/events";
import type { BranchState, ThreadChanges } from "../data";
import type { ViewItem } from "./viewItems";
import { buildInventory } from "./sourceItems";

const taskItem = (content: string): TaskItem => ({
  id: content,
  content,
  status: "pending",
});

const planDoc = (status: PlanStatus, revision = 1): PlanDocument => ({
  title: "Rewrite the parser",
  body: "## Why\nIt is slow.",
  steps: ["read it", "change it"],
  status,
  revision,
});

const command = (phase: HostCommand["phase"]): HostCommand => ({
  toolCallId: `c-${phase}`,
  name: "shell_run_command",
  command: "bun run test",
  phase,
});

const viewItem = (withSnapshot: boolean): ViewItem =>
  ({
    key: "k",
    label: "V1",
    isLatest: true,
    ...(withSnapshot ? { snapshot: { snapshotId: "s1" } } : {}),
  }) as ViewItem;

const sources = (over: Partial<SurfaceDeps> = {}) =>
  createSurfaceSources({
    viewItems: () => [],
    tasks: () => [],
    plan: () => null,
    branch: () => null,
    changes: () => null,
    subagents: () => [],
    commands: () => [],
    sources: () => buildInventory([]),
    browser: () => false,
    ...over,
  });

describe("availability", () => {
  test("the browser is offered only while the thread has one", () => {
    expect(sources().browser.available()).toBe(false);
    expect(sources({ browser: () => true }).browser.available()).toBe(true);
    expect(sources({ browser: () => true }).browser.arrival()).toBe("silent");
  });

  test("nothing is offered on a fresh thread", () => {
    const s = sources();
    expect(s.view.available()).toBe(false);
    expect(s.tasks.available()).toBe(false);
    expect(s.plan.available()).toBe(false);
    expect(s.diff.available()).toBe(false);
    expect(s.files.available()).toBe(false);
    expect(s.commands.available()).toBe(false);
  });

  test("one command is a command log", () => {
    // The first command a thread runs is what brings the surface into existence —
    // there is no threshold, because two is not more of a log than one.
    const s = sources({ commands: () => [command("ok")] });
    expect(s.commands.available()).toBe(true);
    // And it announces rather than steals, *including* when it failed: the terminal in
    // the transcript already opened itself in the turn the operator is reading, and
    // taking the screen away from that is taking it away from the thing being reported.
    expect(s.commands.arrival()).toBe("announce");
    expect(
      sources({ commands: () => [command("error")] }).commands.arrival(),
    ).toBe("announce");
  });

  test("an empty task list is not a task list", () => {
    expect(sources({ tasks: () => [] }).tasks.available()).toBe(false);
    expect(
      sources({ tasks: () => [taskItem("do the thing")] }).tasks.available(),
    ).toBe(true);
  });

  test("the plan surface waits on a plan, which most threads never have", () => {
    expect(sources({ plan: () => null }).plan.available()).toBe(false);
    expect(sources({ plan: () => planDoc("pending") }).plan.available()).toBe(
      true,
    );
    // Still offered once answered: a plan the operator approved is what the thread is
    // working to, and taking it away the moment it was agreed would hide the reference
    // exactly when it starts being used.
    expect(sources({ plan: () => planDoc("approved") }).plan.available()).toBe(
      true,
    );
  });

  test("the changes wait on a branch or a changed project, which most threads never have", () => {
    expect(sources({ branch: () => null }).diff.available()).toBe(false);
    expect(
      sources({
        branch: () => ({ branch: "x" }) as BranchState,
      }).diff.available(),
    ).toBe(true);
    const changed: ThreadChanges = {
      worktree: false,
      projects: [
        { projectId: "p", name: "api", homePath: "~/projects/api", files: [] },
      ],
    };
    expect(sources({ changes: () => changed }).diff.available()).toBe(true);
  });

  test("files need a worktree, not a captured snapshot", () => {
    // The fixture is ordered to fight the old rule: a thread with a snapshot and no
    // branch used to be the *only* way to get this panel, and is now the case that
    // must not. What makes a workspace browsable is having one.
    expect(
      sources({
        branch: () => null,
        viewItems: () => [viewItem(true)],
      }).files.available(),
    ).toBe(false);
    expect(
      sources({
        branch: () => ({ branch: "x" }) as BranchState,
        viewItems: () => [],
      }).files.available(),
    ).toBe(true);
    // The View itself is happy with either, and is unchanged by this.
    expect(
      sources({ viewItems: () => [viewItem(false)] }).view.available(),
    ).toBe(true);
  });
});

describe("arrival", () => {
  test("a plan waiting on an answer puts itself on screen", () => {
    expect(sources({ plan: () => planDoc("pending") }).plan.arrival()).toBe(
      "steal",
    );
  });

  test("a plan that has been answered only announces", () => {
    // Approved, rejected, or being revised, it is a record rather than a question —
    // and a record has nobody waiting on it.
    for (const status of ["approved", "denied", "revising"] as PlanStatus[]) {
      expect(sources({ plan: () => planDoc(status) }).plan.arrival()).toBe(
        "announce",
      );
    }
  });

  test("no plan interrupts nobody", () => {
    expect(sources({ plan: () => null }).plan.arrival()).toBe("announce");
  });

  test("a plan resubmitted after feedback is a new arrival", () => {
    const first = sources({
      plan: () => planDoc("pending", 1),
    }).plan.claimKey();
    const second = sources({
      plan: () => planDoc("pending", 2),
    }).plan.claimKey();
    expect(first).not.toBe(second);
  });

  test("the same plan re-rendering is not", () => {
    expect(sources({ plan: () => planDoc("pending", 3) }).plan.claimKey()).toBe(
      sources({ plan: () => planDoc("pending", 3) }).plan.claimKey(),
    );
  });

  test("the task list never interrupts — the transcript already narrates it", () => {
    const s = sources({ tasks: () => [taskItem("a")] });
    expect(s.tasks.arrival()).toBe("announce");
    expect(s.tasks.claimKey()).toBe("");
  });

  test("the View opens once a thread, and the quiet surfaces stay quiet", () => {
    const s = sources();
    expect(s.view.arrival()).toBe("steal");
    // A constant key is what makes it once-per-thread rather than once-per-item.
    expect(s.view.claimKey()).toBe("");
    expect(s.diff.arrival()).toBe("announce");
    expect(s.files.arrival()).toBe("silent");
  });
});

describe("who wins the focus when two surfaces arrive together", () => {
  /** A claim that always succeeds — the one-shot bookkeeping is the room's, and none of
   *  these cases are about whether a surface has already used its claim. */
  const always = () => true;

  test("the plan is the pane the operator lands on, not the View behind it", () => {
    // The case this rule exists for: a plan-mode turn that also produced an artifact
    // leaves both claiming a steal. Both open; only the plan takes the focus, because
    // it is the one waiting on an answer. Focusing each in turn gave it to whichever
    // was evaluated last, which is how an approval landed the operator on an empty View.
    const claims = arrivalClaims(
      sources({
        plan: () => planDoc("pending"),
        viewItems: () => [viewItem(true)],
      }),
      always,
    );
    expect(claims.map((c) => c.id)).toEqual(["plan", "view"]);
    expect(claims.filter((c) => c.focus).map((c) => c.id)).toEqual(["plan"]);
  });

  test("a lone arrival still takes the focus", () => {
    const claims = arrivalClaims(
      sources({ viewItems: () => [viewItem(true)] }),
      always,
    );
    expect(claims).toEqual([{ id: "view", focus: true }]);
  });

  test("a surface that only announces never claims", () => {
    // Tasks and Agents have content here and neither may interrupt.
    const claims = arrivalClaims(
      sources({ tasks: () => [taskItem("read it")] }),
      always,
    );
    expect(claims).toEqual([]);
  });

  test("a spent claim is skipped, and the next in order takes the focus", () => {
    const claims = arrivalClaims(
      sources({
        plan: () => planDoc("pending"),
        viewItems: () => [viewItem(true)],
      }),
      (key) => !key.startsWith("plan:"),
    );
    expect(claims).toEqual([{ id: "view", focus: true }]);
  });
});
