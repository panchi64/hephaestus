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
 * This file is the dispatcher: it holds the guards every event passes and hands the event
 * to the family that owns it (`./fold/`). Each family returns whether it recognised the
 * event, so an unknown type is simply dropped.
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

import type { RunEvent } from "~/lib/stream";
import { FOLD_RUN_KIND, foldCompaction } from "./fold/compaction";
import { foldLifecycle } from "./fold/lifecycle";
import { foldMessages } from "./fold/messages";
import { foldMeta } from "./fold/meta";
import { foldTools } from "./fold/tools";
import type { FoldContext, FoldDeps } from "./fold/types";

export { FOLD_RUN_KIND };
export type { FoldDeps, FoldState, RunClock } from "./fold/types";

/** The event families, each a handler that says whether the event was its own. */
const FAMILIES: ((ev: RunEvent, c: FoldContext) => boolean)[] = [
  foldTools,
  foldMeta,
  foldMessages,
  foldCompaction,
  foldLifecycle,
];

export function createFolder(
  deps: FoldDeps,
): (anchorId: string, ev: RunEvent, epoch?: number) => void {
  const { state } = deps;
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
    const c: FoldContext = {
      deps,
      // Events land on the current fold target: the drive's placeholder until a
      // `message.injected` boundary retargets to a fresh assistant bubble.
      assistantId: state.foldTarget ?? anchorId,
      // Replayed history folds into state but announces nothing — see `replayThrough`.
      announce: ev.seq > state.replayThrough,
      refetchedFor,
    };
    // A type belongs to exactly one family; the first to recognise it ends the walk.
    for (const family of FAMILIES) if (family(ev, c)) return;
  };
}
