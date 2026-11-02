import { parseInstant } from "~/lib/format";
import type { TraceSpan } from "./model";

/** One row of a turn's waterfall: the span, how deep it nests, and where its bar sits
 *  as a share (0–100) of the turn's whole span of time. */
export interface WaterfallRow {
  span: TraceSpan;
  depth: number;
  offset: number;
  width: number;
}

export interface Waterfall {
  rows: WaterfallRow[];
  totalMs: number;
}

/** Saved spans with the live ones not yet saved, each span once. */
export function mergeSpans(saved: TraceSpan[], live: TraceSpan[]): TraceSpan[] {
  const seen = new Set(saved.map((s) => s.spanId));
  return [...saved, ...live.filter((s) => !seen.has(s.spanId))];
}

/** Rows in tree order — each span followed by its children, siblings by start time —
 *  positioned against the earliest start and latest end among them. A span whose
 *  parent isn't in the set (a background agent the turn started) is a root. */
export function layoutWaterfall(spans: TraceSpan[]): Waterfall {
  if (spans.length === 0) return { rows: [], totalMs: 0 };
  const starts = new Map(
    spans.map((s) => [s.spanId, parseInstant(s.startedAt)]),
  );
  const startOf = (s: TraceSpan): number => starts.get(s.spanId) ?? 0;
  const t0 = Math.min(...spans.map(startOf));
  const t1 = Math.max(...spans.map((s) => startOf(s) + s.durationMs));
  const totalMs = Math.max(t1 - t0, 1);

  const ids = new Set(spans.map((s) => s.spanId));
  const children = new Map<string | null, TraceSpan[]>();
  for (const span of spans) {
    const parent =
      span.parentSpanId !== null && ids.has(span.parentSpanId)
        ? span.parentSpanId
        : null;
    const list = children.get(parent) ?? [];
    list.push(span);
    children.set(parent, list);
  }
  for (const list of children.values())
    list.sort((a, b) => startOf(a) - startOf(b));

  const rows: WaterfallRow[] = [];
  const visit = (parent: string | null, depth: number): void => {
    for (const span of children.get(parent) ?? []) {
      rows.push({
        span,
        depth,
        offset: ((startOf(span) - t0) / totalMs) * 100,
        width: (span.durationMs / totalMs) * 100,
      });
      visit(span.spanId, depth + 1);
    }
  };
  visit(null, 0);
  return { rows, totalMs };
}
