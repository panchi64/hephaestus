import { describe, expect, it } from "bun:test";
import { toStats } from "./summaries";
import type { RunMetricsDTO } from "./wire";

function metrics(over: Partial<RunMetricsDTO> = {}): RunMetricsDTO {
  return {
    steps: 2,
    tool_calls: 1,
    turns: 1,
    input_tokens: 10_000,
    output_tokens: 200,
    cache_read_tokens: 8_000,
    cache_write_tokens: 1_000,
    cost_usd: 0.012,
    saved_usd: 0.028,
    llm_ms: null,
    tool_ms: null,
    ttft_ms_total: null,
    ttft_samples: 0,
    cache_hit_ratio: 0.8,
    ttft_avg_ms: null,
    output_tokens_per_second: null,
    context_window: null,
    context_used: null,
    context: null,
    last_request: {
      route: "anthropic:claude-opus-5-5",
      input_tokens: 5_000,
      output_tokens: 100,
      cache_read_tokens: 4_000,
      cache_write_tokens: 900,
      cost_usd: 0.006,
      cache_miss_reason: "system_changed",
      cache_missed_tokens: 4_100,
    },
    ...over,
  };
}

describe("toStats", () => {
  it("carries the cost, the saving and the cache diagnosis", () => {
    const stats = toStats(metrics());
    expect([stats.costUsd, stats.savedUsd]).toEqual([0.012, 0.028]);
    expect(stats.lastRequest?.costUsd).toBe(0.006);
    expect(stats.lastRequest?.cacheMissReason).toBe("system_changed");
    expect(stats.lastRequest?.cacheMissedTokens).toBe(4_100);
  });

  it("leaves an unpriced thread unpriced rather than free", () => {
    const stats = toStats(metrics({ cost_usd: null, saved_usd: null }));
    expect([stats.costUsd, stats.savedUsd]).toEqual([null, null]);
  });
});
