/**
 * Traced calls: each finished agent run, model request and tool call of the run. They
 * belong to the run, not to a message, so they go to the trace view rather than a block.
 */

import type { RunEvent } from "~/lib/stream";
import type { FoldContext } from "./types";

/** Fold one span event; false when the event is not in this family. */
export function foldSpans(ev: RunEvent, c: FoldContext): boolean {
  if (ev.type !== "span.completed") return false;
  const runId = c.deps.state.activeRunId;
  if (runId !== null) c.deps.onSpan?.(runId, ev);
  return true;
}
