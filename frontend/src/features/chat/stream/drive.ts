/**
 * Driving one run's SSE to its end — and picking one back up that got away.
 *
 * Everything a turn needs *after* the backend has accepted it: open the reader, fold what
 * arrives onto the right bubble, and tear the whole thing down exactly once when it ends.
 * What a frame means is `fold.ts`'s, what to send is the controller's; this file only ever
 * answers "is a turn in flight, and where is it up to".
 *
 * **Detached is not ended.** When the transport exhausts its reconnect budget the run may
 * still be alive server-side, so the drive leaves the turn in a third state — not
 * streaming, not settled — and *skips its teardown*. `sending` and the active run id keep
 * reporting it as in flight, which is what leaves the composer guarded and gives the
 * visibility/online listeners something to reattach to. Only a cancel, a supersede, or a
 * successful reattach settles it.
 *
 * **A generation counter guards every teardown.** A reattach or a thread switch supersedes
 * the drive in flight; that drive's `finally` must not clear state the new one now owns.
 * This is the difference between the two ways to stop: `supersede` orphans the running
 * drive (a new owner is taking over), while `stop` aborts the reader but lets that drive
 * finish tearing its own turn down — which is what a cancel wants, since the turn really
 * is over and still needs reconciling.
 *
 * **Each run restarts the sequence at 1.** So the fold's high-water mark is re-anchored
 * per drive: a fresh turn to 0, and a reattach to the last seq it folded, which replays
 * only the gap. Carrying the previous run's mark over suppresses the new turn's opening
 * events and the answer streams in blank.
 */

import { batch, createSignal, onCleanup, type Accessor } from "solid-js";
import { produce, type SetStoreFunction } from "solid-js/store";
import { StreamDetachedError, streamRun, type RunEvent } from "~/lib/stream";
import { toast } from "~/ui";
import type { ActiveRun, ChatMessage } from "../model";
import { FOLD_RUN_KIND, type FoldState } from "./fold";
import { nextId, type PatchById } from "./patch";

/** What the drive is allowed to touch. Passed in rather than closed over, so the run
 *  lifecycle can be reasoned about without the store, the seam and the composer's
 *  state all being in scope at once. */
export interface RunDriveDeps {
  messages: ChatMessage[];
  setMessages: SetStoreFunction<ChatMessage[]>;
  patchById: PatchById;
  /** The run-scoped bookkeeping, shared by reference with the folder. */
  state: FoldState;
  /** `epoch` is the drive's own; the fold drops an event whose epoch is not current. */
  foldEvent: (assistantId: string, ev: RunEvent, epoch: number) => void;
  setSending: (value: boolean) => void;
  setErrored: (value: boolean) => void;
  titlePending: Accessor<boolean>;
  setTitlePending: (value: boolean) => void;
  /** Reveal the thread's auto-title once the backend has finished writing it. */
  collectTitle: (conversationId: string) => Promise<void>;
  /** Whether the run's events left something queued that a turn will be started for. */
  deliveryDue: () => boolean;
  /** Read the inbox again, and pick up the turn the backend starts for what was queued
   *  (`due`, as the run left it, or anything the read finds). */
  followQueued: (due: boolean) => Promise<void>;
  /** The thread the stream is bound to, read once a turn has settled. */
  conversationId: () => string | null;
  /** Reseat the transcript from the backend's rows once the run has ended — the turn's
   *  ids, versions and stop marker are the backend's. Late-bound: the reconcilers need
   *  `reattachRun`, so they are built after this and reached through a thunk. */
  reseatAfterRun: () => Promise<void>;
  /** Render what the backend has when a reattach folds nothing. Late-bound for the
   *  same reason. */
  recoverLostRun: () => Promise<void>;
  /** Re-read the thread when the row a reattach needs to seat after is not in the
   *  transcript, returning the run as the backend now describes it. Late-bound too. */
  refetchRun: (runId: string) => Promise<ActiveRun | null>;
  onConversationStarted?: (id: string) => void;
  onTurnComplete?: () => void;
}

/** Where a reattach replays from, and — when the caller knows it — what kind of run it
 *  is. Without a kind the reattach falls back to the kind the stream last saw. */
export interface ReattachOptions {
  fromSeq: number;
  kind?: string;
  /** The last event that had already happened when the attach was decided — events up
   *  to it are history being caught up on, and fold without announcing themselves. */
  replayThrough?: number;
  /** The persisted row the run is answering. A reattach that has no streaming bubble of
   *  its own seats a fresh one directly after this row. */
  promptMessageId?: string | null;
}

/** How to attach to a run the persisted detail reports as active: replay its whole
 *  buffer, and treat everything the detail already knew about as history. */
export function attachOptions(run: ActiveRun): ReattachOptions {
  return {
    fromSeq: 0,
    kind: run.kind,
    replayThrough: run.lastSeq,
    promptMessageId: run.promptMessageId,
  };
}

/** Where a run's streaming answer belongs in the transcript: directly after the row it
 *  answers, which the backend names. `null` when that row is not in the transcript (or
 *  the run names none) — the caller re-reads the thread rather than guessing a place. */
export function seatAfterPrompt(
  messages: readonly ChatMessage[],
  promptMessageId: string | null | undefined,
): number | null {
  if (!promptMessageId) return null;
  const at = messages.findIndex((m) => m.id === promptMessageId);
  return at < 0 ? null : at + 1;
}

/** Settle the title throbber when a run ends. A thread still waiting on its name keeps
 *  it while the backend finishes, since a name that lands after the run's stream closed
 *  is never announced on it. The clear only applies if the operator is still on that
 *  thread: switching away cleared it already, and another thread may now own it. */
export async function awaitLateTitle(
  conversationId: string | null,
  deps: Pick<
    RunDriveDeps,
    "titlePending" | "setTitlePending" | "collectTitle" | "conversationId"
  >,
): Promise<void> {
  if (!deps.titlePending() || conversationId === null) {
    deps.setTitlePending(false);
    return;
  }
  try {
    await deps.collectTitle(conversationId);
  } catch {
    // Cosmetic: the rail picks the name up on its next refresh.
  } finally {
    if (deps.conversationId() === conversationId) deps.setTitlePending(false);
  }
}

export interface RunDrive {
  /** True while the live run's transport is detached (reconnect budget exhausted) —
   *  the run may still be alive server-side, awaiting a re-attach rather than over. */
  detached: Accessor<boolean>;
  /** True while a reattach (replay from a known run) is folding in — drives the
   *  "RESYNCING…" affordance, distinct from a fresh turn's `sending`. */
  reattaching: Accessor<boolean>;
  driveRun: (
    runId: string,
    assistantId: string,
    wasNew?: boolean,
    fromSeq?: number,
    onConnected?: () => void,
    replayThrough?: number,
  ) => Promise<void>;
  reattachRun: (runId: string, opts: ReattachOptions) => Promise<void>;
  /** Take ownership away from whatever drive is running: abort its reader and
   *  orphan its teardown, because the caller is about to own this state. */
  supersede: () => void;
  /** Stop reading, but leave the running drive's teardown to it — the turn is over
   *  and still has to be settled and reconciled. */
  stop: () => void;
}

export function createRunDrive(deps: RunDriveDeps): RunDrive {
  const [detached, setDetached] = createSignal(false);
  const [reattaching, setReattaching] = createSignal(false);
  let controller: AbortController | null = null;
  // Bumped whenever a drive is superseded (a reattach aborts the prior reader, or a
  // thread switch tears it down). A driveRun whose generation is stale skips its
  // teardown so an aborted stalled reader can't clear the state — or refetch the
  // wrong thread — out from under the drive that replaced it.
  let driveGen = 0;
  // Whether a drive is mid-flight — started and neither settled nor superseded. Read
  // only by `supersede`, to tell leaving a live turn from merely switching threads.
  let inFlight = false;
  // The conversation a brand-new thread's first turn created, until the room has
  // adopted its id. Held here rather than in the drive that happened to start it: a
  // reattach replaces that drive, and it is the one that sees the turn end.
  let pendingAdoption: string | null = null;

  /** Drive a started run to completion: open the SSE, fold every event onto the
   *  given assistant message, and on end clear streaming/sending, fire the
   *  lifecycle callbacks, and refresh the session list. Shared by `send` and the
   *  branching ops (regenerate/edit) so the run tail lives in one place.
   *  `wasNew` reports a freshly-created conversation so its id can be adopted. */
  async function driveRun(
    runId: string,
    assistantId: string,
    wasNew = false,
    fromSeq?: number,
    onConnected?: () => void,
    replayThrough = 0,
  ): Promise<void> {
    const myGen = ++driveGen;
    inFlight = true;
    if (wasNew) pendingAdoption = deps.conversationId();
    // The fold listens to this drive and no other from here on.
    deps.state.epoch = myGen;
    deps.state.replayThrough = replayThrough;
    deps.state.activeRunId = runId;
    deps.setErrored(false); // a fresh run supersedes any prior failure
    setDetached(false); // a fresh run/reattach supersedes any prior detach
    // Re-anchor the fold high-water mark to this run's sequence (see the header).
    deps.state.maxFoldedSeq = fromSeq ?? 0;
    // Events start folding onto the placeholder; a `message.injected` boundary
    // retargets this as the run's segments split.
    deps.state.foldTarget = assistantId;
    deps.patchById(assistantId, (m) => {
      m.runId = runId;
      m.detached = false; // a fresh drive/reattach supersedes any prior detach
    });
    let connected = false;
    let detachedNow = false;
    // A reattach's backlog, held until it is complete: folded one frame at a time, every
    // frame of a long run re-renders the transcript, so it lands in one batch instead.
    let backlog: RunEvent[] = [];
    const flushBacklog = () => {
      const held = backlog;
      backlog = [];
      if (myGen !== driveGen || !held.length) return;
      batch(() => {
        for (const ev of held) deps.foldEvent(assistantId, ev, myGen);
      });
    };
    try {
      controller = new AbortController();
      try {
        await streamRun(runId, {
          signal: controller.signal,
          fromSeq,
          onEvent: (ev: RunEvent) => {
            // Superseded between the read and here: the bubble and the fold belong to
            // whatever took over.
            if (myGen !== driveGen) return;
            // First event = the transport is live again; let a reattach drop its
            // "RESYNCING…" badge here, so it shows only across the reconnect latency.
            // It's also the queued→streaming transition: the backend only starts
            // emitting once the run actually clears the concurrency semaphore, so
            // the first frame (of any kind) is what tells us it's no longer queued.
            if (!connected) {
              connected = true;
              deps.patchById(assistantId, (m) => (m.queued = false));
              onConnected?.();
            }
            if (ev.seq < replayThrough) {
              backlog.push(ev);
              return;
            }
            if (backlog.length) {
              backlog.push(ev);
              flushBacklog();
            } else deps.foldEvent(assistantId, ev, myGen);
          },
        });
      } finally {
        // The stream can end or drop before the backlog's last frame arrives.
        flushBacklog();
      }
    } catch (err) {
      if (myGen !== driveGen) {
        // superseded — nothing to surface
      } else if (err instanceof StreamDetachedError) {
        // The transport gave up reconnecting, but the run may still be alive
        // server-side: don't treat the turn as ended. Leave it in a distinct
        // "detached" state (not streaming, not settled) with a re-attach
        // affordance, and keep `activeRunId`/`sending` as-is so the composer
        // stays guarded and the visibility/online resume listeners still see
        // an in-flight run to reattach to.
        detachedNow = true;
        setDetached(true);
        deps.patchById(deps.state.foldTarget ?? assistantId, (m) => {
          m.streaming = false;
          m.queued = false;
          m.detached = true;
        });
        toast.error(
          "Connection lost. The response may still be running — reconnect to continue.",
        );
      } else {
        toast.error(
          (err as { detail?: string })?.detail ?? "Unable to reach Talos.",
        );
      }
    } finally {
      // Skip teardown when superseded by a reattach/thread-switch (that drive
      // owns the state now, and clearing it here would race it) or when this
      // drive ended detached — the run isn't actually over, so `activeRunId`/
      // `sending` must keep reporting it as in-flight until it's re-attached,
      // cancelled, or superseded.
      if (myGen === driveGen && !detachedNow) {
        inFlight = false;
        deps.state.activeRunId = null;
        deps.patchById(deps.state.foldTarget ?? assistantId, (m) => {
          m.streaming = false;
          m.detached = false; // the turn is genuinely over — clear any stale banner
          m.queued = false; // defensive: covers a resolve with no frames ever folded
        });
        // Read before any await: by the time the transcript is reseated the backend may
        // have emptied the queue into the turn this has to go looking for.
        const due = deps.deliveryDue();
        deps.setSending(false);
        const conversationId = deps.conversationId();
        void awaitLateTitle(conversationId, deps);
        // Keyed on the id the first turn created rather than on `wasNew`: when a
        // reattach took this turn over, the drive that sees it end is not the one that
        // started it, and the thread's id would never have been adopted.
        if (pendingAdoption !== null && pendingAdoption === conversationId) {
          deps.onConversationStarted?.(conversationId);
        }
        pendingAdoption = null;
        deps.onTurnComplete?.();
        // Take the backend's account of the turn just recorded. The live messages carry
        // client ids and no version count, so without this the ‹k/n› cycler never
        // appears and a later regenerate/edit/delete/pin would address an id the
        // backend doesn't know.
        await deps.reseatAfterRun();
        await deps.followQueued(due);
      }
    }
  }

  /** Reattach to a run that's already streaming (or just finished) and fold what
   *  we missed, then continue live. Two callers:
   *  - resume after a stalled/dropped transport (a backgrounded tab): the
   *    assistant message still exists, so replay from `fromSeq` = the last seq we
   *    folded — only the tail re-applies (the seq guard drops the overlap).
   *  - a cold read mid-stream (page reload): the answer has no turn yet, so seed an
   *    empty one bound to the run and replay the whole buffer (`fromSeq` 0).
   *  Reuses `driveRun`, so the shared finally clears streaming/sending and
   *  reconciles the persisted turn once the run ends.
   *
   *  **The seeded turn goes after the row the backend names, and nothing else is
   *  invented.** The operator's prompt is persisted as the turn begins, so the thread
   *  already holds it; only the streaming answer is missing. If the named row is not in
   *  the transcript this holds (the run began after it was loaded), the thread is
   *  re-read once rather than a prompt-less bubble being drawn into the wrong place.
   *
   *  **A fold seeds nothing.** It writes no answer, so there is no assistant turn to
   *  find or to seed — its frames seat their own compaction turn — and a seeded one
   *  would sit in the transcript as an empty, streaming bubble for the whole fold. */
  async function reattachRun(
    runId: string,
    opts: ReattachOptions,
  ): Promise<void> {
    // Abort a stalled/old reader first so it can't keep folding beside the new one
    // (its drive is superseded by the generation bump inside the next driveRun).
    controller?.abort();
    controller = null;
    // The *last* matching turn: a steered run splits into several assistant
    // segments sharing one runId, and only the newest is the live one. A fold
    // anchors to no turn at all (`""`), so neither arm below touches one.
    let assistantId =
      (opts.kind ?? deps.state.runKind) === FOLD_RUN_KIND
        ? ""
        : deps.messages.findLast(
            (m) => m.runId === runId && m.role === "assistant",
          )?.id;
    const seeded = assistantId === undefined;
    if (assistantId === undefined) {
      assistantId = nextId("a");
      let at = seatAfterPrompt(deps.messages, opts.promptMessageId);
      if (at === null) {
        // The transcript does not hold the row this run answers: read the thread once
        // and take the backend's word for where the answer goes. Still missing after
        // that (a run with no prompt on record yet) means the end of what it holds.
        const boundTo = deps.conversationId();
        const live = await deps.refetchRun(runId);
        // The operator moved on while the read was out: this run belongs to a thread
        // that is no longer on screen, and seating its bubble here would plant it in
        // whichever thread is.
        if (deps.conversationId() !== boundTo) return;
        at =
          seatAfterPrompt(deps.messages, live?.promptMessageId) ??
          deps.messages.length;
      }
      const assistantMsg: ChatMessage = {
        id: assistantId,
        role: "assistant",
        content: "",
        blocks: [],
        streaming: true,
        runId,
        createdAt: new Date().toISOString(),
      };
      deps.setMessages(produce((m) => m.splice(at, 0, assistantMsg)));
    } else if (assistantId) {
      deps.patchById(assistantId, (m) => {
        m.streaming = true;
        m.detached = false; // re-attaching supersedes the "connection lost" banner
      });
    }
    // `driveRun` re-anchors `maxFoldedSeq` to the `fromSeq` passed below, so the
    // resume replays only the gap after the last folded event.
    // Reattaching first: the transcript reads it on the `sending` edge to tell a resumed
    // run from a send, which would let go of the thread's opening follow.
    setReattaching(true);
    deps.setSending(true);
    try {
      // Clears RESYNCING on the first folded event; the finally is the safety net
      // for a reattach that never connects (e.g. an immediate 404).
      await driveRun(
        runId,
        assistantId,
        false,
        opts.fromSeq,
        () => setReattaching(false),
        opts.replayThrough,
      );
    } finally {
      setReattaching(false);
    }
    // Skip the recovery when the attempt itself ended detached (reconnect budget
    // exhausted, not a 404/empty buffer) — the run may still be alive, so reseating
    // from the (possibly reply-less) persisted detail here would discard the
    // re-attach affordance for no reason; leave the seed detached and let the
    // operator/resume retry.
    if (!detached() && seeded && deps.state.maxFoldedSeq === opts.fromSeq)
      await deps.recoverLostRun();
  }

  onCleanup(() => controller?.abort());

  return {
    detached,
    reattaching,
    driveRun,
    reattachRun,
    supersede: () => {
      controller?.abort();
      controller = null;
      // Orphan the in-flight drive's teardown, and move the fold's epoch with it so a
      // frame already read for that drive is dropped rather than folded into whatever
      // the room is bound to next.
      deps.state.epoch = ++driveGen;
      setReattaching(false);
      setDetached(false);
      // Leaving a live turn skips its teardown (above), and the list is what would have
      // been refreshed by it — the new thread's row has to appear, and a run still going
      // has to show as running. The id is deliberately *not* adopted: the operator went
      // elsewhere, and adopting would seat them back on the thread they just left.
      pendingAdoption = null;
      if (inFlight) {
        inFlight = false;
        deps.onTurnComplete?.();
      }
    },
    stop: () => {
      controller?.abort();
      controller = null;
      setDetached(false);
    },
  };
}
