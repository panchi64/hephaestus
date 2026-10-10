/**
 * One SSE event, applied to the transcript.
 *
 * This is the whole of the live-to-store translation, and it is deliberately the only
 * thing in this file. The controller around it owns the *run* — opening the reader,
 * cancelling, reattaching, persisting — and none of that belongs in a switch whose single
 * question is "what does this frame change on screen". Splitting them means a new event
 * type is a case here and nothing else, and a change to how a run is driven never risks
 * touching how a delta lands.
 *
 * **Three invariants everything below depends on.**
 *
 * *Idempotence.* A reattach replays a run's buffer from an earlier seq, so events arrive
 * that were already applied — sometimes over a transcript that was cold-loaded with them
 * baked in. `seq` is monotonic per run, so the high-water mark drops the overlap; where a
 * frame can also arrive over a *persisted* row (the compaction divider, a snapshot chip, a
 * steering bubble) it is additionally deduped on its own durable id.
 *
 * *One fold target, which can move.* Events land on the assistant bubble the drive opened,
 * until a `message.injected` boundary closes it and opens a fresh one — mirroring how the
 * backend persists a steered turn as segments around the injected user message, so the
 * live transcript and a reload agree about the shape of the turn.
 *
 * *Conversation-scoped things are not message blocks.* The plan, the snapshot list and the
 * window meter outlive the run that announced them; folding them onto a message would
 * strand a plan on the turn that happened to create it, the next time the transcript
 * replays.
 */

import type { SetStoreFunction } from "solid-js/store";
import { produce } from "solid-js/store";
import { CONTEXT_OVERFLOW_AFTER_FOLD_DETAIL } from "~/lib/stream";
import type { ContextWindow, RunEvent, TaskItem } from "~/lib/stream";
import { ENGAGEMENT_ORDER, permissionLevel } from "../model";
import type { PermissionLevel, PlanDocument } from "../model";
import { toast } from "~/ui";
import {
  commandBoundary,
  commandReason,
  toTerminalOutcome,
} from "../data/hostCommands";
import { startedTool, stringifyResult, toolImages } from "../data/messages";
import { toStats } from "../data/summaries";
import { toVersionChipBlock, toViewSnapshotRef } from "../data/viewSnapshots";
import { refreshSessions } from "../data/sessions";
import { revealTitle } from "../data/titleReveals";
import { requestFoldAnchor } from "../foldAnchor";
import type {
  ChatMessage,
  Citation,
  ConversationStats,
  HostCommand,
  ViewSnapshotRef,
} from "../model";
import { isTerminalTool } from "../toolPresentation";
import { describeToolResult } from "../toolSummary";
import { placeToolCall } from "../toolTree";
import {
  appendDelta,
  clearPark,
  findHost,
  findReview,
  findTool,
  nextId,
  upsertHost,
  type PatchById,
} from "./patch";

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
  /** A settled fold named a boundary turn this transcript does not hold — it is behind
   *  the backend. The fold will not guess a position; the caller re-reads the
   *  persisted thread instead. Optional for a caller that never folds. */
  onFoldMisplaced?: () => void;
}

/** When the streaming run began and how far it has got, both straight off the wire:
 *  `startedAt` is `run.started`'s own `ts` — the backend's instant, never a client
 *  stopwatch — and `step` the index of the newest `step.started`, which the backend
 *  counts from 1 within the run. Null until the first step opens. */
export interface RunClock {
  startedAt: string;
  step: number | null;
}

/** The transcript id of the fold a `compaction.started` at ``seq`` opened.
 *
 *  Derived rather than generated so a replay re-seats the same turn instead of a second
 *  one: `seq` is monotonic per run and is the only per-event identifier that survives a
 *  reattach, which replays the whole buffer from 0. */
export function liveFoldId(seq: number): string {
  return `compaction-live-${seq}`;
}

/** The run kind a hand-started fold is submitted as — the backend's `LANE_BY_KIND` key.
 *  Read here to report why a fold declined, and by the drive to skip seeding an
 *  assistant turn for a run that writes none. */
export const FOLD_RUN_KIND = "compaction";

/** Remove every fold still in flight — the live turns `compaction.started` seated.
 *  Both of the frames that end one (its own settle, and the run's terminal) do this. */
function dropLiveFolds(list: ChatMessage[]): void {
  for (let i = list.length - 1; i >= 0; i -= 1)
    if (list[i].role === "compaction" && list[i].compaction) list.splice(i, 1);
}

export function createFolder(
  deps: FoldDeps,
): (anchorId: string, ev: RunEvent, epoch?: number) => void {
  const { state, patchById, setMessages } = deps;
  // The checkpoints already re-read the thread for, so a backend that keeps naming a
  // turn this client cannot find costs one refetch rather than a loop.
  const refetchedFor = new Set<string>();

  return function foldEvent(
    anchorId: string,
    ev: RunEvent,
    epoch?: number,
  ): void {
    // Ownership: an event stamped by a drive that is no longer the current one belongs
    // to a run the room has since left.
    if (epoch !== undefined && epoch !== state.epoch) return;
    // Idempotency: `seq` is monotonic per run, so an event at or below the high-
    // water mark was already folded (a reattach replay overlapping a still-live
    // reader). Skipping it stops a re-applied `answer.delta` from doubling text.
    if (ev.seq <= state.maxFoldedSeq) return;
    state.maxFoldedSeq = ev.seq;
    // Replayed history folds into state but announces nothing — see `replayThrough`.
    const announce = ev.seq > state.replayThrough;
    // Events land on the current fold target: the drive's placeholder until a
    // `message.injected` boundary retargets to a fresh assistant bubble.
    const assistantId = state.foldTarget ?? anchorId;
    switch (ev.type) {
      case "thinking.delta":
        patchById(assistantId, (m) => appendDelta(m, "thinking", ev.text));
        break;
      case "answer.delta":
        patchById(assistantId, (m) => appendDelta(m, "text", ev.text));
        break;
      case "tool.started":
        // A command the operator watches run is a terminal, not a generic tool card.
        // Which tools those are is the table's answer, not a name test here — see
        // `ToolEntry.terminal`. (tool.started fires before approval.required, so this
        // seeds the pending terminal.) A call a script made is never one: it nests on
        // the script's card as a compact row, whatever tool it is.
        if (!ev.parent_tool_call_id && isTerminalTool(ev.name)) {
          patchById(assistantId, (m) =>
            upsertHost(m, ev.tool_call_id, ev.name, {
              command:
                typeof ev.args.command === "string" ? ev.args.command : "",
              explanation: commandReason(ev.args),
            }),
          );
          break;
        }
        patchById(assistantId, (m) =>
          placeToolCall(
            m.blocks ?? (m.blocks = []),
            (id) => `tool-${id}`,
            startedTool(ev.tool_call_id, ev.name, ev.args),
            ev.parent_tool_call_id,
          ),
        );
        break;
      case "tool.progress":
        // One frame, two readings, and **the block that already exists is what says
        // which**. A terminal is a command printing as it runs, so its frames are
        // *deltas* arriving every half-second and they APPEND — dropping the previous
        // piece would leave a terminal showing only whatever the last half-second
        // happened to print. Everything else sends a status note ("starting the
        // sandbox"), which is one fact restated, so it REPLACES.
        //
        // Reading it off the block rather than off the tool name is what keeps this
        // from being a second copy of the tool table: the event carries no name, and a
        // call's own block already encodes the answer the table gave when the call
        // started.
        patchById(assistantId, (m) => {
          const host = findHost(m, ev.tool_call_id);
          if (host) {
            if (ev.partial)
              host.command.streamed =
                (host.command.streamed ?? "") + ev.partial;
            // Seconds since the spawn, from the run rather than from a clock here.
            // The settled result carries the authoritative figure and overwrites it.
            if (ev.elapsed_s != null)
              host.command.elapsedMs = Math.round(ev.elapsed_s * 1000);
            return;
          }
          const t = findTool(m, ev.tool_call_id);
          if (t) {
            t.progress = ev.partial ?? undefined;
            if (ev.elapsed_s != null)
              t.elapsedMs = Math.round(ev.elapsed_s * 1000);
          }
        });
        break;
      case "tool.completed": {
        // A call with a result is waiting on nobody — retire the prompt it parked on,
        // so a replay doesn't re-ask what was already answered.
        patchById(assistantId, (m) => clearPark(m, ev.tool_call_id));
        if (!ev.parent_tool_call_id && isTerminalTool(ev.name)) {
          // The result is the record: it has the streams apart, where the ticks that
          // streamed in had them concatenated. So the accumulated text is dropped on
          // the same patch that replaces it — leaving it would be two copies of one
          // command's output, one of them worse.
          const outcome = toTerminalOutcome(ev.result);
          // No outcome and a sentence back means a guard refused the call outright —
          // the wrong mode, a host that cannot fence — and it never ran. The cold read
          // projects the same string the same way, so a reload agrees with what the
          // operator watched; without this arm a refused command sat lit as running
          // for the rest of the thread.
          const patch: Partial<HostCommand> | null =
            outcome ??
            (typeof ev.result === "string" && ev.result
              ? { phase: "denied", error: ev.result }
              : null);
          if (patch)
            patchById(assistantId, (m) =>
              upsertHost(m, ev.tool_call_id, ev.name, {
                ...patch,
                streamed: undefined,
              }),
            );
          break;
        }
        patchById(assistantId, (m) => {
          const t = findTool(m, ev.tool_call_id);
          if (t) {
            t.status = "ok";
            t.result = stringifyResult(ev.result);
            t.outcome = describeToolResult(ev.name, ev.result);
            t.progress = undefined; // the run is over — drop the spin-up note
            t.images = toolImages(ev.images);
            // The same read the cold mapper does, so a backgrounded command's
            // declaration and fence survive a reload identically.
            t.boundary = commandBoundary(ev.result);
          }
        });
        break;
      }
      case "tool.failed":
        // A failure settles the call too — same retirement as the completed case.
        patchById(assistantId, (m) => clearPark(m, ev.tool_call_id));
        if (!ev.parent_tool_call_id && isTerminalTool(ev.name)) {
          patchById(assistantId, (m) =>
            upsertHost(m, ev.tool_call_id, ev.name, {
              phase: "error",
              error: ev.error,
            }),
          );
          break;
        }
        patchById(assistantId, (m) => {
          const t = findTool(m, ev.tool_call_id);
          if (t) {
            t.status = "error";
            t.error = ev.error;
            t.progress = undefined; // the run is over — drop the spin-up note
          }
        });
        break;
      case "context.injected":
        // A block the chassis put in front of the model. It lands on the rail in the
        // order it happened — which is ahead of the work it shaped, since the turn's
        // context is assembled before the model sees any of it. Keyed by `seq` because
        // the same contributor can legitimately inject twice in one turn (a plan that
        // grew a task between steps is a new injection, not a repeat), and `seq` is the
        // only identifier on the wire that is unique per event and stable across a
        // replay.
        patchById(assistantId, (m) => {
          (m.blocks ?? (m.blocks = [])).push({
            kind: "context",
            id: `ctx-${ev.seq}`,
            injection: {
              contributor: ev.contributor,
              placement: ev.placement,
              tokens: ev.tokens,
              text: ev.text,
              truncated: ev.truncated,
            },
          });
        });
        break;
      case "compaction.started": {
        // The thread has stopped to fold its own history, and that takes tens of seconds
        // on a local endpoint — so it becomes a turn in the transcript *now*, the way an
        // assistant turn opens on its first delta rather than once the answer is whole.
        //
        // A turn rather than a row on the assistant's rail, which is where this used to
        // live. Two things were wrong with that at once: a fold the operator started by
        // hand has no assistant turn to hang off, so that path drew nothing anywhere; and
        // where there *was* one, the same fold appeared twice — once as a rail row and
        // again as the divider it settled into.
        //
        // Keyed by `seq`, the only per-event id stable across a replay. One turn can
        // legitimately fold twice (the threshold before it ran, then an overflow retry
        // inside it), and those are two pauses rather than one repeated.
        const live: ChatMessage = {
          id: liveFoldId(ev.seq),
          role: "compaction",
          content: "",
          createdAt: ev.ts,
          compaction: {
            reason: ev.reason,
            messages: ev.messages,
            tokensEstimate: ev.tokens_estimate,
          },
        };
        setMessages(
          produce((list) => {
            if (list.some((m) => m.id === live.id)) return;
            list.push(live);
          }),
        );
        break;
      }
      case "compaction.delta":
        // The summary as the model writes it. Appended to the live turn only — it is the
        // model's working, not the checkpoint, and what the operator keeps is the parsed
        // summary the settle below seats in its place.
        setMessages(
          produce((list) => {
            // The delta carries no id of its own, so the target is the fold still in
            // flight — searched from the end, because a turn that folds twice has an
            // older, settled one above it and the newest is always the one writing.
            for (let i = list.length - 1; i >= 0; i -= 1) {
              const live = list[i];
              if (live.role !== "compaction" || !live.compaction) continue;
              live.compaction.summary =
                (live.compaction.summary ?? "") + ev.text;
              return;
            }
          }),
        );
        break;
      case "compaction.failed":
        // The fold the opening frame announced is not going to land, so the live turn it
        // seated comes back out — an unsettled fold is not a fold, and the model's raw
        // working must not stay on screen as though it were the summary. The thread is
        // otherwise untouched.
        setMessages(produce(dropLiveFolds));
        if (announce)
          toast.error(
            ev.message ||
              `Compaction (${ev.reason}) failed — the conversation was not changed.`,
          );
        break;
      case "review.started":
        // The chassis is about to answer for the operator. The row opens now rather than
        // on the verdict, so a review that costs a model call reads as work in flight —
        // and so it lands ahead of the tool row it judges, which is where it belongs.
        patchById(assistantId, (m) => {
          if (findReview(m, ev.tool_call_id)) return;
          (m.blocks ?? (m.blocks = [])).push({
            kind: "review",
            id: `review-${ev.tool_call_id}`,
            review: {
              toolCallId: ev.tool_call_id,
              name: ev.name,
              summary: ev.summary,
              // What the reviewer was given beyond the one-line summary, for the tools
              // that have any. Undefined rather than null, so the card renders the block
              // only when there is one.
              detail: ev.detail ?? undefined,
              reach: ev.reach ?? undefined,
            },
          });
        });
        break;
      case "review.completed":
        patchById(assistantId, (m) => {
          const b = findReview(m, ev.tool_call_id);
          if (!b) return;
          b.review.decision = ev.decision;
          b.review.stage = ev.stage;
          b.review.reason = ev.reason;
          // How the call was bounded, not merely whether it was allowed: the tier says
          // which structural ground cleared it, and `fenced` says whether this host could
          // hold it there at all — the answer to "why did an ordinary command still ask?"
          b.review.tier = ev.tier ?? undefined;
          b.review.fenced = ev.fenced;
          // Null on the wire means the model stage never ran — the deterministic judge
          // cleared it, or there was nothing to review with. Undefined here so the card
          // renders the axes only when there are axes.
          b.review.risk = ev.risk ?? undefined;
          b.review.authorization = ev.authorization ?? undefined;
          b.review.correctness = ev.correctness ?? undefined;
        });
        break;
      case "tasks.updated":
        // Whole-list replace, not a merge: the event is full state, which is what makes
        // it idempotent when the stream is replayed from an earlier seq on reconnect.
        state.tasksRevision += 1;
        deps.setTasks(ev.items);
        break;
      case "plan.updated":
        // The document, not the checklist. Same full-state reasoning; the counter is
        // separate because the two arrive independently and a shared one would make each
        // backfill think the other had overtaken it.
        state.planRevision += 1;
        deps.setPlan({
          title: ev.title,
          body: ev.body,
          steps: ev.steps,
          status: ev.status,
          revision: ev.revision,
        });
        break;
      case "permission.changed":
        // The level moved from inside the run. Seated as though it had been read off the
        // thread, because that is what it now is: the row says so, and the composer must
        // send this level rather than the one it was holding.
        //
        // Validated rather than cast, and that matters *because* it is sent back: a level
        // this build has no rule for would be seated, ridden on the next message, and
        // refused at the edge — the operator's message failing for a reason nothing on
        // screen explains. `permissionLevel` degrades an unreadable value to the
        // strictest one, which is the only reading that cannot widen a thread.
        deps.setPermission(permissionLevel(ev.level));
        break;
      case "approval.required": {
        // `args` is typed as always-present, but it arrives as untrusted JSON off
        // the wire — default it once here, in the mapper, so no consumer of the
        // stored block has to guard a `Object.keys(args)` or an `args.command`.
        const args: Record<string, unknown> = ev.args ?? {};
        // At Auto the chassis tried to answer this call first, and what it found rides
        // onto whichever card ends up asking: a park is now the whole of what an
        // unrecoverable act produces, so the reviewer's `too_destructive` would otherwise
        // be visible only on a collapsed row above the prompt being answered. Both
        // surfaces take it, because the commands that earn that word are shell commands
        // and those are the ones that render as a terminal.
        if (isTerminalTool(ev.name)) {
          patchById(assistantId, (m) => {
            const review = findReview(m, ev.tool_call_id)?.review;
            upsertHost(m, ev.tool_call_id, ev.name, {
              command: typeof args.command === "string" ? args.command : "",
              explanation: ev.explanation ?? undefined,
              risk: review?.risk,
              reviewReason: review?.reason,
              phase: "pending",
            });
          });
          break;
        }
        patchById(assistantId, (m) => {
          const review = findReview(m, ev.tool_call_id)?.review;
          (m.blocks ?? (m.blocks = [])).push({
            kind: "approval",
            id: `approval-${ev.tool_call_id}`,
            approval: {
              toolCallId: ev.tool_call_id,
              name: ev.name,
              args,
              summary: ev.summary,
              explanation: ev.explanation ?? undefined,
              risk: review?.risk,
              reviewReason: review?.reason,
            },
          });
        });
        break;
      }
      case "question.asked": {
        // Same defaulting discipline as `approval.required` above: `questions` is typed
        // as always-present but arrives as untrusted JSON, so it is defaulted once here
        // rather than guarded at every consumer of the stored block.
        patchById(assistantId, (m) => {
          (m.blocks ?? (m.blocks = [])).push({
            kind: "question",
            id: `question-${ev.tool_call_id}`,
            question: {
              toolCallId: ev.tool_call_id,
              questions: (ev.questions ?? []).map((q) => ({
                question: q.question,
                multiSelect: q.multi_select ?? false,
                options: (q.options ?? []).map((o) => ({
                  label: o.label,
                  description: o.description ?? undefined,
                })),
              })),
            },
          });
        });
        break;
      }
      case "question.answered": {
        // Marked, not removed — the block stops being a park (nothing left for the dock
        // to collect) and starts being a row in the transcript. The words of each
        // question ride on the event rather than being read off the block, because the
        // backend pairs them from the parked call's own arguments and a cold load is
        // projected from the same pairing: warm and reloaded must not be two renderings.
        //
        // Found across the transcript rather than on the fold target, for the same reason
        // the dock stopped keying its park on "the last streaming message": a
        // `message.injected` boundary retargets the fold, so the question can sit on the
        // segment *before* the one events are landing on now. Addressed by its call id,
        // which is unique, so the wider search cannot hit the wrong block.
        //
        // An empty list is deliberately not written: a malformed call that parsed to no
        // questions would otherwise be kept as a card with a heading and nothing under it.
        if ((ev.answers ?? []).length > 0)
          setMessages(
            produce((list) => {
              for (const m of list) {
                const block = m.blocks?.find(
                  (b) =>
                    b.kind === "question" &&
                    b.question.toolCallId === ev.tool_call_id,
                );
                if (block?.kind === "question") {
                  block.question.answers = (ev.answers ?? []).map((a) => ({
                    question: a.question,
                    selections: a.selections ?? [],
                    text: a.text ?? undefined,
                  }));
                  return;
                }
              }
            }),
          );
        break;
      }
      case "view.live": {
        // One live head per *conversation*, not per turn: clear any prior live
        // block (it may sit on an earlier turn) before marking this turn's, so a
        // replaced or stopped server never lingers as a stale LIVE head once the
        // viewport aggregates view items across the whole transcript.
        const live = { url: ev.url, title: ev.title ?? undefined };
        setMessages(
          produce((list) => {
            for (const m of list)
              if (m.blocks)
                m.blocks = m.blocks.filter((b) => b.kind !== "view_live");
            const m = list.find((x) => x.id === assistantId);
            if (m)
              (m.blocks ?? (m.blocks = [])).push({
                kind: "view_live",
                id: nextId("view-live"),
                live,
              });
          }),
        );
        break;
      }
      case "view.live.stopped":
        // The live head is conversation-scoped and close usually arrives a turn or
        // more after it started — drop it wherever it lives, not just on this run.
        setMessages(
          produce((list) => {
            for (const m of list)
              if (m.blocks)
                m.blocks = m.blocks.filter((b) => b.kind !== "view_live");
          }),
        );
        break;
      case "view.snapshot": {
        // A version minted by `show`: append to the conversation-scoped version list
        // (the panel), deduped since a reattach replay can re-deliver the event.
        const ref = toViewSnapshotRef(ev);
        deps.setSnapshots((prev) =>
          prev.some((s) => s.snapshotId === ref.snapshotId)
            ? prev
            : [...prev, ref],
        );
        // Fold an inline transcript chip only for a *static* preview (a `show(file=…)`).
        // A live/auto version (served head) is already marked by its `view_live` chip,
        // so a second chip for the same action would just be visual duplication.
        if (ref.preview) {
          const chip = toVersionChipBlock(assistantId, {
            snapshotId: ref.snapshotId,
            title: ref.title,
            previewKind: ref.preview.kind,
          });
          patchById(assistantId, (m) => {
            const blocks = m.blocks ?? (m.blocks = []);
            if (!blocks.some((b) => b.id === chip.id)) blocks.push(chip);
          });
        }
        break;
      }
      case "message.queued": {
        // A message the backend accepted into this run. Usually it tags the optimistic
        // bubble `send` already pushed (matched by text, first untagged wins so duplicate
        // texts pair off in order); on a reattach replay there is no optimistic bubble, so
        // rebuild it from the event.
        //
        // A sub-agent's report rides the same road and must not read the same. It gets no
        // optimistic bubble to tag — nobody typed it — and it lands as a `subagent` row, so
        // the live transcript says what a reload says rather than attributing to the
        // operator words they have not even seen. It also means the report is not offered
        // the edit/withdraw affordances, which belong to a message its author can still
        // take back.
        const fromSubagent = ev.source === "subagent";
        setMessages(
          produce((list) => {
            if (list.some((m) => m.queuedMessageId === ev.message_id)) return;
            const untagged = fromSubagent
              ? undefined
              : list.find(
                  (m) =>
                    m.queuedPending &&
                    !m.queuedMessageId &&
                    m.content === ev.text,
                );
            if (untagged) untagged.queuedMessageId = ev.message_id;
            else
              list.push({
                id: nextId(fromSubagent ? "s" : "u"),
                role: fromSubagent ? "subagent" : "user",
                content: ev.text,
                queuedPending: true,
                queuedMessageId: ev.message_id,
                createdAt: ev.ts,
              });
          }),
        );
        break;
      }
      case "message.edited":
        // The operator rewrote a still-pending bubble. Usually `editQueued`
        // already applied the text optimistically-on-success; this fold makes a
        // reattach replay (and any second tab) converge on the same content. An
        // already-injected message is part of the turn and never changes.
        setMessages(
          produce((list) => {
            const bubble = list.find(
              (m) => m.queuedMessageId === ev.message_id && m.queuedPending,
            );
            if (bubble) bubble.content = ev.text;
          }),
        );
        break;
      case "message.held":
        // The run is holding this one back (or has stopped). Folded rather than left to
        // the request that asked for it, so a second tab — and a reattaching client
        // replaying the stream — see a stalled queue for what it is.
        setMessages(
          produce((list) => {
            const bubble = list.find(
              (m) => m.queuedMessageId === ev.message_id && m.queuedPending,
            );
            if (bubble) bubble.queuedHeld = ev.held;
          }),
        );
        break;
      case "message.withdrawn":
        // Only a still-pending bubble is removable — an already-injected message
        // is part of the turn and must never vanish from the transcript.
        setMessages(
          produce((list) => {
            const i = list.findIndex(
              (m) => m.queuedMessageId === ev.message_id && m.queuedPending,
            );
            if (i >= 0) list.splice(i, 1);
          }),
        );
        break;
      case "message.injected":
        // The queued message reached the model: promote its bubble to a normal
        // user turn and split the assistant flow around it, mirroring how the
        // backend persists the steered turn (…assistant segment, user message,
        // next assistant segment…) so the live transcript and a reload agree.
        setMessages(
          produce((list) => {
            const qi = list.findIndex(
              (m) => m.queuedMessageId === ev.message_id && m.queuedPending,
            );
            if (qi < 0) return;
            const [bubble] = list.splice(qi, 1);
            bubble.queuedPending = false;
            const target = list.find((m) => m.id === assistantId);
            const targetIsFresh =
              target && !target.blocks?.length && !target.content;
            if (targetIsFresh && list[list.length - 1] === target) {
              // A batch of injections at one boundary shares one fresh segment:
              // slot this message before the placeholder a prior injection opened.
              list.splice(list.length - 1, 0, bubble);
            } else {
              if (target) target.streaming = false;
              list.push(bubble);
              const fresh: ChatMessage = {
                id: nextId("a"),
                role: "assistant",
                model: target?.model,
                content: "",
                blocks: [],
                streaming: true,
                runId: state.activeRunId ?? undefined,
                createdAt: new Date().toISOString(),
              };
              list.push(fresh);
              state.foldTarget = fresh.id;
            }
          }),
        );
        break;
      case "conversation.compacted": {
        // Conversation-level, like the title above — but it *is* a message, so it goes
        // into the list. Placed after the turn the backend named rather than appended:
        // a divider at the bottom would claim to have folded the turns it kept, and a
        // reload (which places it chronologically) would then disagree with the live view.
        const divider: ChatMessage = {
          id: ev.message_id,
          role: "compaction",
          content: ev.summary,
          createdAt: ev.ts,
          foldedMessages: ev.messages_compacted,
          tokensBefore: ev.tokens_before ?? undefined,
          tokensAfter: ev.tokens_after ?? undefined,
          // Defaulted rather than left absent: an older backend that sends no reason
          // can only have meant the ordinary one, and a divider is worse for having a
          // segment that appears and disappears with the backend version.
          compactionReason: ev.reason ?? "threshold",
          // The backend derives these from the very `summary` on this frame, and the
          // cold read parses the same stored string with the same function — so the
          // divider seated here and the one a reload seats are the same divider.
          summarySections: ev.sections ?? undefined,
        };
        // Idempotent on `message_id`: a reattach replays the run's whole buffer
        // (`fromSeq: 0`) over a transcript that was cold-loaded *with* this turn
        // already in it, so an unguarded splice would seat a second identical one —
        // and re-announce a fold that happened minutes ago.
        let inserted = false;
        let misplaced = false;
        setMessages(
          produce((list) => {
            // The live turn goes, whether or not the settled one is seated below: it is
            // the model's raw working and this frame carries the real thing. Dropped
            // even on the idempotent path, since a replay re-opens it from the same
            // `compaction.started` and nothing else would ever close it.
            dropLiveFolds(list);
            if (list.some((m) => m.id === divider.id)) return;
            // The backend's boundary is authoritative; a null one is its own statement
            // that the checkpoint follows everything. A boundary naming a turn this
            // transcript does not hold is not a licence to guess — appended at the
            // bottom the divider would claim to have folded turns it kept — so the
            // thread is re-read instead (below).
            if (!ev.after_message_id) {
              list.push(divider);
              inserted = true;
              return;
            }
            const at = list.findIndex((m) => m.id === ev.after_message_id);
            if (at < 0) {
              misplaced = true;
              return;
            }
            list.splice(at + 1, 0, divider);
            inserted = true;
          }),
        );
        if (misplaced && !refetchedFor.has(divider.id)) {
          refetchedFor.add(divider.id);
          deps.onFoldMisplaced?.();
        }
        if (inserted) {
          // Take the operator to it. The live turn they were watching sat at the
          // tail; this one belongs at the fold boundary, which is usually off screen
          // above — so without this the thing they were waiting for arrives by
          // disappearing.
          if (announce) requestFoldAnchor(divider.id);
          // A fold that lands mid-answer scrolls past unseen — and it changes what the
          // model can still see, which is not something to discover later by reading
          // back. The checkpoint is the durable record; this is the notification — and
          // only for a fold that just happened, not one a replay is catching up on.
          // `messages_compacted` counts messages, not exchanges — say messages.
          if (announce)
            toast.info(
              ev.messages_compacted > 0
                ? `Context compacted — ${ev.messages_compacted} earlier ${ev.messages_compacted === 1 ? "message is" : "messages are"} now a summary for the model.`
                : "Context compacted — earlier messages are now a summary for the model.",
            );
        }
        break;
      }
      case "conversation.titled":
        // Conversation-level, not message-level: hand it to the typewriter reveal
        // rather than folding onto the assistant message.
        revealTitle(ev.conversation_id, ev.title);
        // And pull the list now rather than at the end of the turn, as
        // `conversation.linked` does below and for the same reason: the thread
        // exists, it has a name, and the rail claiming otherwise for the length of a
        // long run is a lie about state the backend has already settled. It is also
        // what the header's static title falls back to once the reveal finishes.
        refreshSessions();
        // The throbber answers "is the backend naming this thread", and it has just
        // stopped being true. It used to be left to the run's `finally`, because the
        // reveal could not render until the room adopted the new id — so clearing it
        // here would have shown a bare "New conversation" for the rest of the turn.
        // The surfaces resolve against the live id now, so the name arrives in the
        // same beat the throbber goes.
        deps.setTitlePending(false);
        break;
      case "conversation.linked":
        // The turn spawned a thread of its own. Pull the list now rather than at
        // the end of the turn: the new thread is already running, and a session
        // that exists but isn't listed for another few minutes reads as work that
        // went nowhere. The toast is the account of *why* a row appeared.
        refreshSessions();
        if (announce)
          toast.info(
            ev.title
              ? `Research thread started — ${ev.title}`
              : "Research thread started.",
          );
        break;
      case "run.error":
        if (announce) toast.error(ev.message || "The run failed.");
        patchById(assistantId, (m) => (m.streaming = false));
        deps.setErrored(true);
        break;
      case "run.metrics":
        // The backend derives the window's fullness; the meter just renders it.
        // Authoritative either way: a null context (this turn ran on a windowless
        // model, or reported no usage) clears a stale reading rather than keeping it.
        // *Absent* is not null — a frame that carries no `context` field at all says
        // nothing about the window, and reading it as a clear would drop the meter on
        // a frame that merely was not about it.
        if (ev.context !== undefined) deps.setUsage(ev.context);
        deps.setStats(toStats(ev));
        break;
      case "citation.added":
        patchById(assistantId, (m) => {
          const citations = m.citations ?? (m.citations = []);
          // One source can arrive several times — listed by a search, then read by a
          // fetch — and the backend says which sighting is the stronger claim. So this
          // replaces rather than skips: keep the row's position (it is the display
          // number) and take the higher rung's detail.
          const next: Citation = {
            url: ev.url,
            title: ev.title ?? undefined,
            key: ev.key,
            kind: ev.kind,
            engagement: ev.engagement,
            snippet: ev.snippet ?? undefined,
            published: ev.published ?? undefined,
            retrievedAt: ev.retrieved_at ?? undefined,
            sourceId: ev.source_id ?? undefined,
            ref: ev.ref ?? undefined,
          };
          const at = citations.findIndex((c) => (c.key ?? c.url) === ev.key);
          if (at < 0) citations.push(next);
          else if (
            ENGAGEMENT_ORDER[ev.engagement] >
            ENGAGEMENT_ORDER[citations[at].engagement ?? "listed"]
          )
            citations[at] = next;
        });
        break;
      case "limit.notice":
        // A bound on the turn. "verify" is a transient "re-attempting…" progress note,
        // not a stop — leave it silent. The rest stopped the run, so surface why.
        //
        // "context" gets a sentence of our own on the end, and it is the only limit
        // that does: the backend's message says what the model refused and what the
        // chassis already tried, but the remedy the operator has *here* is a control on
        // the stopped turn, and only the frontend knows what that control is called.
        // Naming it in the toast is what connects the thing that just happened to the
        // button that answers it — the toast is transient, the marker is not.
        //
        // Only where the control is actually offered, though. A turn that already folded
        // and still overran carries the after-fold marker, `BlockedFooter` withholds the
        // button, and a toast naming it would contradict the sentence it is appended to.
        if (announce && ev.limit !== "verify")
          toast.error(
            ev.limit === "context" &&
              ev.detail !== CONTEXT_OVERFLOW_AFTER_FOLD_DETAIL
              ? `${ev.message} Compact and retry on the stopped turn to fold this thread and carry on.`
              : ev.message,
          );
        break;
      case "run.ended": {
        // A blocked outcome is a real stopping point, not a normal finish —
        // leave a persistent marker on the turn (the limit.notice toast alone
        // vanishes, and a reload would otherwise show a turn that just stops).
        if (ev.outcome === "blocked")
          patchById(assistantId, (m) => {
            m.blocked = true;
            m.blockedDetail = ev.detail ?? undefined;
          });
        // A fold still in flight at the terminal never landed, so its turn goes: an
        // unsettled fold is not a fold, and the raw working it was showing is not
        // something to leave on screen as though it were the summary. Ordinarily there
        // is nothing to drop — a fold inside a chat turn settled long before the turn
        // ended — so this is the abandoned case, which is where the *reason* matters.
        setMessages(produce(dropLiveFolds));
        // The backend's own sentence, which is the only place the distinction lives:
        // "nothing new since the last fold", "the summarizer wrote nothing" and "the
        // conversation moved under it" are three different things to have happened and
        // lead to three different next moves.
        //
        // Guarded on the run's *kind*, not on whether a live turn was dropped. A fold
        // that declines before it announces itself has nothing to abandon — and that is
        // the single most likely way for this button to do nothing, so keying the report
        // on the abandoned turn would go quiet in exactly the case that needs a sentence.
        if (
          announce &&
          deps.state.runKind === FOLD_RUN_KIND &&
          ev.outcome === "blocked" &&
          ev.detail
        )
          toast.info(ev.detail);
        // Spent: the next run announces its own kind, and one attached past its
        // `run.started` must not inherit this one's.
        deps.state.runKind = null;
        deps.setRunClock?.(() => null);
        break;
      }
      case "run.started":
        deps.state.runKind = ev.kind;
        deps.setRunClock?.(() => ({ startedAt: ev.ts, step: null }));
        break;
      case "step.started":
        // Only onto a clock this run started: a reattach past `run.started` has no
        // start instant to pair the step with, and a step alone is not a clock.
        deps.setRunClock?.((prev) => prev && { ...prev, step: ev.index });
        break;
      // step.completed: no store change
    }
  };
}
