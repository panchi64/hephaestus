import { createStore, produce } from "solid-js/store";
import type { TraceSpan } from "./model";

/** Runs whose live spans are kept; older ones are read back from the saved trace. */
const KEPT_RUNS = 20;

const [spansByRun, setSpansByRun] = createStore<
  Record<string, TraceSpan[] | undefined>
>({});
/** Kept runs, oldest first, with the span ids each already holds. */
const kept = new Map<string, Set<string>>();

/** Record a span the run's stream just reported. A replayed stream reports it again;
 *  that copy is dropped. */
export function recordLiveSpan(runId: string, span: TraceSpan): void {
  const seen = kept.get(runId);
  if (seen?.has(span.spanId)) return;
  if (seen) {
    seen.add(span.spanId);
    setSpansByRun(produce((all) => all[runId]?.push(span)));
    return;
  }
  kept.set(runId, new Set([span.spanId]));
  setSpansByRun(
    produce((all) => {
      all[runId] = [span];
      for (const oldest of kept.keys()) {
        if (kept.size <= KEPT_RUNS) break;
        kept.delete(oldest);
        delete all[oldest];
      }
    }),
  );
}

/** What the run's stream has reported so far, in arrival order. */
export function liveSpans(runId: string): TraceSpan[] {
  return spansByRun[runId] ?? [];
}
