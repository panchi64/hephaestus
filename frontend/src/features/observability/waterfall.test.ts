import { describe, expect, test } from "bun:test";
import { turnAt, type TraceSpan, type TurnSummary } from "./model";
import { layoutWaterfall, mergeSpans } from "./waterfall";

const T0 = Date.parse("2026-11-02T12:00:00Z");

function span(
  spanId: string,
  startMs: number,
  durationMs: number,
  parentSpanId: string | null = null,
): TraceSpan {
  return {
    spanId,
    traceId: "t",
    parentSpanId,
    kind: parentSpanId ? "model" : "run",
    agentName: "main",
    model: "m",
    toolName: null,
    startedAt: new Date(T0 + startMs).toISOString(),
    durationMs,
    ttftMs: null,
    inputTokens: null,
    outputTokens: null,
    cacheReadTokens: null,
    cacheWriteTokens: null,
    costUsd: null,
    errorType: null,
  };
}

describe("layoutWaterfall", () => {
  test("nests children under their parent in start order", () => {
    const { rows, totalMs } = layoutWaterfall([
      span("b", 600, 400, "root"),
      span("root", 0, 1000),
      span("a", 100, 400, "root"),
    ]);
    expect(totalMs).toBe(1000);
    expect(rows.map((r) => [r.span.spanId, r.depth])).toEqual([
      ["root", 0],
      ["a", 1],
      ["b", 1],
    ]);
    expect(rows[1]!.offset).toBe(10);
    expect(rows[1]!.width).toBe(40);
  });

  test("a span whose parent is outside the set is its own root", () => {
    const { rows } = layoutWaterfall([
      span("root", 0, 500),
      span("x", 200, 100, "elsewhere"),
    ]);
    expect(rows.map((r) => r.depth)).toEqual([0, 0]);
  });

  test("an empty trace has no rows", () => {
    expect(layoutWaterfall([])).toEqual({ rows: [], totalMs: 0 });
  });
});

describe("turnAt", () => {
  const turn = (runId: string, startMs: number): TurnSummary => ({
    runId,
    startedAt: new Date(T0 + startMs).toISOString(),
    durationMs: 500,
    modelCalls: 1,
    toolCalls: 0,
    inputTokens: null,
    outputTokens: null,
    costUsd: null,
    chatCostUsd: null,
    errors: 0,
  });
  const turns = [turn("r2", 10_000), turn("r1", 0)];

  test("picks the latest turn that started at or before the moment", () => {
    expect(turnAt(turns, new Date(T0 + 12_000).toISOString())?.runId).toBe(
      "r2",
    );
    expect(turnAt(turns, new Date(T0 + 9_000).toISOString())?.runId).toBe("r1");
  });

  test("a moment before every turn has none", () => {
    expect(turnAt(turns, new Date(T0 - 1).toISOString())).toBeNull();
  });
});

describe("mergeSpans", () => {
  test("adds live spans not yet saved, once each", () => {
    const merged = mergeSpans(
      [span("a", 0, 1)],
      [span("a", 0, 1), span("b", 1, 1)],
    );
    expect(merged.map((s) => s.spanId)).toEqual(["a", "b"]);
  });
});
