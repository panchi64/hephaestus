import type { ChatMessage } from "../model";

/**
 * The run the transcript is attached to, **read reactively**.
 *
 * The controller's own `activeRunId` is a field on a plain object, not a signal — so a
 * memo that read it alone would not re-run when it changed, and would go on answering
 * from whichever run happened to be live the last time the messages moved. The messages
 * carry the same fact and are a store: the turn in flight is the last message still
 * streaming or detached, and its `runId` is the run. `fallback`, the controller's value,
 * covers the one moment the messages cannot answer — the placeholder pushed before
 * `run.started` has patched an id onto it.
 */
export function liveRunIdOf(
  messages: readonly ChatMessage[],
  fallback: string | null,
): string | null {
  return messages.findLast((m) => m.streaming || m.detached)?.runId ?? fallback;
}
