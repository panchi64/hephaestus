import type { RunEvent } from "~/lib/stream";

export interface ReplayGate {
  /** Take one event; delivers everything held once the backlog is complete. */
  push: (ev: RunEvent) => void;
  /** Deliver whatever is held — the stream ended or dropped before the backlog did. */
  flush: () => void;
}

/** Hold a reattach's backlog (events up to `replayThrough`) and deliver it in one call:
 *  folded frame by frame, every event of a long run re-renders the transcript. With
 *  nothing to catch up on, each event is delivered as it arrives. */
export function createReplayGate(
  replayThrough: number,
  deliver: (events: RunEvent[]) => void,
): ReplayGate {
  let held: RunEvent[] = [];
  const flush = () => {
    if (!held.length) return;
    const out = held;
    held = [];
    deliver(out);
  };
  return {
    push: (ev) => {
      held.push(ev);
      if (ev.seq >= replayThrough) flush();
    },
    flush,
  };
}
