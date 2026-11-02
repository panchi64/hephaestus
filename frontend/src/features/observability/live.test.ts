import { describe, expect, test } from "bun:test";
import { liveSpans, recordLiveSpan } from "./live";
import type { TraceSpan } from "./model";

function span(spanId: string): TraceSpan {
  return {
    spanId,
    traceId: "t",
    parentSpanId: null,
    kind: "model",
    agentName: "main",
    model: "m",
    toolName: null,
    startedAt: "2026-11-02T12:00:00Z",
    durationMs: 100,
    ttftMs: null,
    inputTokens: null,
    outputTokens: null,
    cacheReadTokens: null,
    cacheWriteTokens: null,
    costUsd: 0.01,
    errorType: null,
  };
}

describe("live spans", () => {
  test("a replayed stream's second report of a span is dropped", () => {
    for (const id of ["a", "b", "a", "b", "c"])
      recordLiveSpan("replayed", span(id));
    expect(liveSpans("replayed").map((s) => s.spanId)).toEqual(["a", "b", "c"]);
  });

  test("only the most recent runs are kept", () => {
    recordLiveSpan("first", span("x"));
    for (let i = 0; i < 25; i++) recordLiveSpan(`later-${i}`, span("y"));
    expect(liveSpans("first")).toEqual([]);
    expect(liveSpans("later-24")).toHaveLength(1);
  });
});
