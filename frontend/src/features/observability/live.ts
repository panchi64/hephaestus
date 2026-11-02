import { createSignal } from "solid-js";
import type { TraceSpan } from "./model";

/** Runs whose live spans are kept; older ones are read back from the saved trace. */
const KEPT_RUNS = 20;

const [spansByRun, setSpansByRun] = createSignal<
  ReadonlyMap<string, TraceSpan[]>
>(new Map());

/** Record a span the run's stream just reported. */
export function recordLiveSpan(runId: string, span: TraceSpan): void {
  setSpansByRun((prev) => {
    const next = new Map(prev);
    const spans = next.get(runId) ?? [];
    next.delete(runId);
    next.set(runId, [...spans, span]);
    while (next.size > KEPT_RUNS) {
      const oldest = next.keys().next().value;
      if (oldest === undefined) break;
      next.delete(oldest);
    }
    return next;
  });
}

/** What the run's stream has reported so far, in arrival order. */
export function liveSpans(runId: string): TraceSpan[] {
  return spansByRun().get(runId) ?? [];
}
