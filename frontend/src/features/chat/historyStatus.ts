/**
 * Where the open thread's history is: not asked for, on its way, failed, or here.
 *
 * The transcript used to have two states — messages, or "Start a conversation" — and a
 * thread whose history was still loading, or had failed to load, looked exactly like a
 * thread with nothing in it. Opening one showed the empty state for the length of every
 * read, and a read that failed showed it for good: no error, nothing to retry, and
 * re-clicking the row did nothing because the selection had not changed.
 *
 * Pure, so the rule is written down once and the room only reads the answer.
 */

/** `idle` is a thread with no history to read — a new one, or the one a first turn is
 *  still creating. `loading` and `error` are about the read; `ready` is the thread. */
export type HistoryStatus = "idle" | "loading" | "error" | "ready";

/** A `createResource`'s `state`, narrowed to the cases that matter here. */
export type ResourceState =
  "unresolved" | "pending" | "ready" | "refreshing" | "errored";

export function historyStatus(input: {
  /** The thread the room is seated on. */
  id: string | null;
  /** The history resource's state. */
  state: ResourceState;
  /** Whether the value it holds is *this* thread's. A resource keeps the last thread's
   *  value when its source goes null or while the next fetch is in flight, so `ready`
   *  alone says nothing about whose history it is. */
  holdsThisThread: boolean;
}): HistoryStatus {
  if (input.id === null) return "idle";
  if (input.state === "errored") return "error";
  if (input.state === "ready" && input.holdsThisThread) return "ready";
  return "loading";
}
