/**
 * The shapes the fold is built from: the run-scoped state it advances, what it is allowed
 * to touch, and the per-event context each family handler is handed. Kept apart from the
 * handlers so every family can import them without reaching back into the dispatcher.
 */

import type { SetStoreFunction } from "solid-js/store";
import type { ContextWindow, TaskItem } from "~/lib/stream";
import type {
  ChatMessage,
  ConversationStats,
  PermissionLevel,
  PlanDocument,
  ViewSnapshotRef,
} from "../../model";
import type { InboxState } from "../../inbox";
import type { PatchById } from "../patch";

/** The run-scoped bookkeeping the fold both reads and advances. Shared by reference
 *  with the controller, which resets it on a thread switch and reads `maxFoldedSeq`
 *  as the resume point for a reattach — one object rather than a getter/setter pair
 *  per field, because these four are always read and reset together. */
export interface FoldState {
  /** The highest event `seq` folded for the current run. Two purposes: the resume
   *  point a reattach replays *after*, and the idempotency guard below. Events are
   *  seq ≥ 1, so 0 means nothing has been folded. */
  maxFoldedSeq: number;
  /** The assistant message events currently fold onto — normally the placeholder the
   *  drive was started with, until a `message.injected` boundary retargets it. */
  foldTarget: string | null;
  /** Bumped on every `tasks.updated`. Plain counter, not a signal: its only job is to
   *  let an in-flight REST backfill notice the stream overtook it. */
  tasksRevision: number;
  /** The same, for `plan.updated`. Separate from the counter above because the two
   *  arrive independently — one shared counter would make each backfill conclude the
   *  stream had overtaken it whenever the *other* one moved. */
  planRevision: number;
  /** The run currently streaming, if any — stamped onto a bubble this fold opens. */
  activeRunId: string | null;
  /** What kind of run is streaming, from its own `run.started`.
   *
   *  Read for exactly one decision: whether a blocked terminal is worth saying out loud.
   *  A chat turn that stops leaves a persistent marker on the turn it stopped, so the
   *  detail is already on screen; a **fold** has no turn to mark, and one that declines
   *  before it announces itself — nothing has been said since the last fold — would
   *  otherwise end in silence, which is the exact failure this surface was built to fix. */
  runKind: string | null;
  /** Which drive the fold is currently listening to. A drive stamps every event it
   *  hands over with the epoch it started under, and the fold drops any whose stamp is
   *  not this one — so a chunk read just before a thread switch cannot write the old
   *  thread's metrics, tasks or queue into the thread now bound. Bumped when a drive
   *  starts and when one is superseded. */
  epoch: number;
  /** Events at or below this `seq` are a replay of what had already happened when the
   *  stream attached — the run's `last_seq` in the detail the attach was decided from.
   *  They fold into state like any other, but say nothing out loud: a toast announces
   *  something that just happened, and these did not. 0 for a run followed from its
   *  start. */
  replayThrough: number;
}

/* Sub-agents used to be folded here, from a `subagent.*` family the blocking delegation
   emitted onto its parent's stream. They are runs of their own now, with transcripts and
   register rows of their own, and the panel reads those — so there is nothing for a fold
   over one parent turn's stream to carry. */

/** Everything the fold is allowed to touch. Passed in rather than reached for, so the
 *  same fold serves the persistent main room and an ephemeral compare pane without
 *  either one knowing the other exists. */
export interface FoldDeps {
  state: FoldState;
  /** The controller's, not a second one: the index hint it keeps only pays off if
   *  every delta goes through the same instance. */
  patchById: PatchById;
  setMessages: SetStoreFunction<ChatMessage[]>;
  /** The conversation's inbox, which the `message.*` and `inbox.waiting` events keep
   *  current. Optional for a caller that never folds one. */
  inbox?: { get: () => InboxState; set: (next: InboxState) => void };
  setSnapshots: (fn: (prev: ViewSnapshotRef[]) => ViewSnapshotRef[]) => void;
  setTasks: (items: TaskItem[]) => void;
  setPlan: (plan: PlanDocument | null) => void;
  /** Re-seat the thread's level after the run moved it. Only `permission.changed` calls
   *  this — an operator-chosen level never comes back through the fold, because the
   *  client is where it came from. */
  setPermission: (level: PermissionLevel) => void;
  setUsage: (context: ContextWindow | null) => void;
  setStats: (stats: ConversationStats | null) => void;
  setErrored: (errored: boolean) => void;
  /** Clear the "the backend is naming this thread" throbber, on the event that makes
   *  it false. The drive's teardown still clears it for a turn that ended without a
   *  name — this is the other exit. */
  setTitlePending: (pending: boolean) => void;
  /** The live run's clock, for the composer's header: when the backend started it and
   *  which model round-trip it is on. Optional — a caller with no header to put it in
   *  (the compare pane) leaves it out, and the fold then drops both facts. */
  setRunClock?: (next: (prev: RunClock | null) => RunClock | null) => void;
  /** Where the run waits for its model endpoint, or null once it has started or ended.
   *  Optional for the same reason as the clock. */
  setRunQueue?: (queue: RunQueue | null) => void;
  /** A settled fold named a boundary turn this transcript does not hold — it is behind
   *  the backend. The fold will not guess a position; the caller re-reads the
   *  persisted thread instead. Optional for a caller that never folds. */
  onFoldMisplaced?: () => void;
  /** The agent attached a browser to the thread. Not called on a replay: a reattach finds
   *  the live session through the room's own read. Optional for a caller with no browser
   *  panel (the compare pane). */
  onBrowserOpened?: () => void;
}

/** When the streaming run began and how far it has got, both straight off the wire:
 *  `startedAt` is `run.started`'s own `ts` — the backend's instant, never a client
 *  stopwatch — and `step` the index of the newest `step.started`, which the backend
 *  counts from 1 within the run. Null until the first step opens. */
export interface RunClock {
  startedAt: string;
  step: number | null;
}

/** A run waiting in line for its model endpoint: the endpoint's name and the run's
 *  1-based place, as the latest `run.queued` said. */
export interface RunQueue {
  endpoint: string;
  position: number;
}

/** What a family handler is handed for one event, after the dispatcher's guards have let
 *  it through. */
export interface FoldContext {
  deps: FoldDeps;
  /** The message the event lands on: the current fold target, falling back to the
   *  drive's placeholder. */
  assistantId: string;
  /** False for a replay of what had already happened when the stream attached — the
   *  event folds into state but says nothing out loud. */
  announce: boolean;
  /** The checkpoints already re-read the thread for, so a backend that keeps naming a
   *  turn this client cannot find costs one refetch rather than a loop. */
  refetchedFor: Set<string>;
}
