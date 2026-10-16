/**
 * What each surface has, and what it should do about getting it.
 *
 * One module, because the alternative is a `switch` on the surface id wherever the
 * question is asked — the header bar asks availability to decide which buttons exist, the
 * panel asks it to decide whether it is worth being open, and the arrival effect asks
 * whether a surface has earned the right to put itself on screen. Three dispatches drift;
 * one does not.
 *
 * **A source reads signals the room already has.** It is handed accessors rather than
 * reaching for a store, and it may not open a fetch of its own: a surface nobody has
 * opened still has its availability evaluated on every render, so a source that polled
 * would make a closed panel cost network. Where a surface's data genuinely needs fetching
 * — the worktree diff — the fetch belongs to whoever already owns it and arrives here as
 * an accessor like the rest.
 *
 * Keyed by `SurfaceId`, so a surface added to the registry without a source is a compile
 * error rather than a button that never appears.
 */

import type { HostCommand, PlanDocument } from "../model";
import type { TaskItem } from "~/lib/stream/events";
import type { Subagent } from "../data";
import type { BranchState, ThreadChanges } from "../data";
import type { SourceInventory } from "./sourceItems";
import { SURFACE_IDS, type SurfaceId } from "./surfaces";
import type { ViewItem } from "./viewItems";

/**
 * What a surface does the first time it has something.
 *
 * - `steal` — put itself on screen. For the case where *not* seeing it leaves the
 *   operator waiting on something they do not know is waiting on them.
 * - `announce` — light its header button and otherwise stay out of the way.
 * - `silent` — appear in the header, and nothing more until asked.
 *
 * It is a **predicate, not a field on the registry**, because at least one surface's
 * answer depends on the moment rather than on what kind of surface it is: a plan that
 * needs approving is an interruption, and the same plan once it has been answered is not.
 */
export type Arrival = "steal" | "announce" | "silent";

export interface SurfaceSource {
  /** Whether the surface has content. A surface that does not is offered nowhere:
   *  no header button, and no place in a restored layout. */
  available: () => boolean;
  arrival: () => Arrival;
  /** What makes *this* arrival distinct from the last. A steal is claimed once per
   *  key per conversation, so a surface cannot reopen itself after a manual close —
   *  while a genuinely new arrival gets a new key and a fresh claim. A constant key
   *  means "once per thread, ever". */
  claimKey: () => string;
}

/** What the sources read. Accessors, never stores — see the module note.
 *
 *  The thread's permission level used to be here, read by the plan source to work out
 *  whether a plan was awaiting approval. It is gone because the plan now carries its own
 *  status, which is the fact that question was always really asking about — and reading
 *  it directly removed the stand-in guard the inference needed. */
export interface SurfaceDeps {
  viewItems: () => ViewItem[];
  tasks: () => TaskItem[];
  plan: () => PlanDocument | null;
  branch: () => BranchState | null | undefined;
  changes: () => ThreadChanges | null | undefined;
  subagents: () => Subagent[];
  commands: () => HostCommand[];
  /** Every source the thread has read, already folded. Derived from the transcript
   *  like `commands`, so this opens no fetch either — see the module note. */
  sources: () => SourceInventory;
  /** Whether the thread has a live browser, or one that has just ended. Held by the room
   *  and read once on open, so this opens no fetch either. */
  browser: () => boolean;
}

export function createSurfaceSources(
  deps: SurfaceDeps,
): Record<SurfaceId, SurfaceSource> {
  return {
    // An empty list is not a list. The agent writes one the moment it has something
    // to write, so "no rows" and "no tasks" are the same state.
    //
    // A sub-agent's list counts, because the surface shows those too (grouped under its
    // name — see `taskGroups.ts`). Read off the thread's own list alone, a thread that
    // delegated all of its work would offer no Tasks button at all while four lists were
    // being worked through inside it.
    tasks: {
      available: () =>
        deps.tasks().length > 0 ||
        deps.subagents().some((s) => s.tasks.length > 0),
      // Work in progress, narrated by the transcript beside it. A panel that opened
      // itself every time a task ticked over is a panel the operator learns to close.
      arrival: () => "announce",
      claimKey: () => "",
    },
    // **A plan interrupts exactly when it is waiting on a yes**, and it can now say so
    // itself. The thread is blocked on an operator who does not know it — the one state
    // worth putting on screen uninvited. Approved, denied, or being revised, it is a
    // record rather than a question, and a record badges.
    //
    // This used to be inferred from `permission() === "plan"`, which needed a guard
    // against the seat's not-yet-loaded stand-in (the stand-in *is* `plan`, so every
    // thread that had ever written a plan popped the panel open for the width of a
    // fetch and spent its one-shot claim). Reading the plan's own status needs no such
    // guard: a plan that has not arrived has no status to be pending.
    plan: {
      available: () => deps.plan() !== null,
      arrival: () => (deps.plan()?.status === "pending" ? "steal" : "announce"),
      // Keyed by the revision, so a plan resubmitted after feedback is a new arrival
      // and earns a fresh claim, while the same plan re-rendering does not.
      claimKey: () => `plan:${deps.plan()?.revision ?? 0}`,
    },
    // A thread that has never launched one has no panel; one that has keeps it for the
    // rest of the session, because a finished sub-agent's transcript is as much a result
    // as a run in flight — and the operator's usual reason for opening this is that
    // something went wrong, which is exactly when the card has stopped being live.
    agents: {
      available: () => deps.subagents().length > 0,
      // Launching one is the agent getting on with work it was already doing. The
      // panel is there when the operator wants it; a sub-agent parked on an approval
      // does interrupt, but through the approval dock, which is where they answer it.
      arrival: () => "announce",
      claimKey: () => "",
    },
    // A thread that has run nothing has no command log, and the first command it runs
    // is what brings the surface into existence — derived from the transcript, so there
    // is no fetch here and none is allowed (see the module note).
    commands: {
      available: () => deps.commands().length > 0,
      // **Announce, even for a failure.** A failed command is worth knowing about and
      // the operator already learns of it where it happened: the terminal in the
      // transcript opens itself, in the turn they are reading. Stealing the panel on
      // top of that would take the screen away from the thing it is telling them about
      // — and would do it on every failing command in a thread that is *expected* to
      // fail commands, which is most of a code thread's working day.
      arrival: () => "announce",
      claimKey: () => "",
    },
    // A thread with a worktree branch, or with changes in the operator's own project
    // folders; every other thread has nothing to review, which is the ordinary case.
    diff: {
      available: () =>
        Boolean(deps.branch()) || (deps.changes()?.projects.length ?? 0) > 0,
      // Changes accumulate for the whole length of a code thread. A panel that opened
      // itself every time the agent touched a file is a panel the operator learns to
      // close reflexively.
      arrival: () => "announce",
      claimKey: () => "",
    },
    // **Browsable from the thread's first turn, not from its first snapshot.** This used
    // to wait on `viewItems().some(i => i.snapshot)`, which meant the panel was offered
    // only once the agent had called `view_show` — so a code thread that never minted a
    // version had no Files panel at all while its worktree sat full of files. A worktree
    // is what makes a workspace browsable; a snapshot is one thing that can be taken of
    // it. Same condition the diff keeps, because it is the same fact.
    files: {
      available: () => Boolean(deps.branch()),
      // Nobody is ever waiting on a file listing.
      arrival: () => "silent",
      claimKey: () => "",
    },
    // The first thing the thread reads brings the inventory into existence — derived
    // from the transcript's citations, so there is no fetch here and none is allowed.
    sources: {
      available: () => deps.sources().items.length > 0,
      // **Announce.** The answer being written above is what the operator is reading,
      // and the sources are what they check it against *afterwards*. A panel that
      // opened itself on the first search hit would take the screen away from the turn
      // producing it, at the earliest possible moment.
      arrival: () => "announce",
      claimKey: () => "",
    },
    // A thread has a browser only once the agent or the operator has started one.
    browser: {
      available: () => deps.browser(),
      // **Silent.** The agent is already narrating what it does in the browser, and the
      // operator driving it is a choice they make from the menu; a panel that opened
      // itself on every page load would be one they learn to close.
      arrival: () => "silent",
      claimKey: () => "",
    },
    // A thread's View is its captured versions plus any live head.
    view: {
      available: () => deps.viewItems().length > 0,
      // The first artifact a thread produces is usually the thing it was asked for,
      // and a panel that stayed shut over it would be hiding the answer. Once per
      // thread, which is what the constant key buys: a later close means "not this".
      arrival: () => "steal",
      claimKey: () => "",
    },
  };
}

/** One surface putting itself on screen, and whether it is the one to look at. */
export interface ArrivalClaim {
  id: SurfaceId;
  /** Whether opening this should also move the operator to it. */
  focus: boolean;
}

/**
 * Which surfaces may open themselves right now, and which single one wins the focus.
 *
 * **Two can arrive in the same pass**, and that is the case this exists for: a plan
 * submitted at the end of a turn that also produced an artifact leaves both the Plan and
 * the View claiming a steal. Both belong on screen. Only one can be the pane the operator
 * is dropped into, and the loop that opened them used to focus each in turn — so the
 * *last* one evaluated won, which is how an operator asked to approve a plan landed on an
 * empty View and had to close the panel to find the plan behind it.
 *
 * Registry order decides, because it is already declared in order of how much the surface
 * can be waiting on the operator (`surfaces.ts`, in that file's own order).
 * A second priority list beside it would be one more thing to keep in step.
 *
 * Pure, and the claim is passed in: the one-shot bookkeeping is the room's (it is keyed
 * per conversation and survives a re-render), and a rule about which pane the operator
 * lands on should be testable without one.
 */
export function arrivalClaims(
  sources: Record<SurfaceId, SurfaceSource>,
  claim: (key: string) => boolean,
): ArrivalClaim[] {
  const claims: ArrivalClaim[] = [];
  for (const id of SURFACE_IDS) {
    const source = sources[id];
    if (!source.available() || source.arrival() !== "steal") continue;
    if (!claim(`${id}:${source.claimKey()}`)) continue;
    claims.push({ id, focus: claims.length === 0 });
  }
  return claims;
}
