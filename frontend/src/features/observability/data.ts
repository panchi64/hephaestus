import { api, isApiError } from "~/lib/api";
import type { SpanFields } from "~/lib/stream/events";
import type { BackgroundActivity, TraceSpan, TurnSummary } from "./model";

interface TurnSummaryDTO {
  run_id: string;
  started_at: string;
  duration_ms: number;
  model_calls: number;
  tool_calls: number;
  input_tokens: number | null;
  output_tokens: number | null;
  cost_usd: number | null;
  chat_cost_usd: number | null;
  errors: number;
}

interface BackgroundActivityDTO {
  days: number;
  agents: {
    agent_name: string;
    calls: number;
    cost_usd: number | null;
    avg_duration_ms: number;
    last_run_at: string;
  }[];
  per_day: { day: string; cost_usd: number }[];
  recent: {
    span_id: string;
    agent_name: string;
    model: string | null;
    conversation_id: string | null;
    started_at: string;
    duration_ms: number;
    cost_usd: number | null;
    error_type: string | null;
  }[];
}

/** The live `span.completed` frame and a saved trace row share this shape. */
export function toSpan(dto: SpanFields): TraceSpan {
  return {
    spanId: dto.span_id,
    traceId: dto.trace_id,
    parentSpanId: dto.parent_span_id,
    kind: dto.kind,
    agentName: dto.agent_name,
    model: dto.model,
    toolName: dto.tool_name,
    startedAt: dto.started_at,
    durationMs: dto.duration_ms,
    ttftMs: dto.ttft_ms,
    inputTokens: dto.input_tokens,
    outputTokens: dto.output_tokens,
    cacheReadTokens: dto.cache_read_tokens,
    cacheWriteTokens: dto.cache_write_tokens,
    costUsd: dto.cost_usd,
    errorType: dto.error_type,
  };
}

function toTurn(dto: TurnSummaryDTO): TurnSummary {
  return {
    runId: dto.run_id,
    startedAt: dto.started_at,
    durationMs: dto.duration_ms,
    modelCalls: dto.model_calls,
    toolCalls: dto.tool_calls,
    inputTokens: dto.input_tokens,
    outputTokens: dto.output_tokens,
    costUsd: dto.cost_usd,
    chatCostUsd: dto.chat_cost_usd,
    errors: dto.errors,
  };
}

function toBackground(dto: BackgroundActivityDTO): BackgroundActivity {
  return {
    days: dto.days,
    agents: dto.agents.map((a) => ({
      agentName: a.agent_name,
      calls: a.calls,
      costUsd: a.cost_usd,
      avgDurationMs: a.avg_duration_ms,
      lastRunAt: a.last_run_at,
    })),
    perDay: dto.per_day.map((d) => ({ day: d.day, costUsd: d.cost_usd })),
    recent: dto.recent.map((c) => ({
      spanId: c.span_id,
      agentName: c.agent_name,
      model: c.model,
      conversationId: c.conversation_id,
      startedAt: c.started_at,
      durationMs: c.duration_ms,
      costUsd: c.cost_usd,
      errorType: c.error_type,
    })),
  };
}

export async function fetchConversationTurns(
  conversationId: string,
): Promise<TurnSummary[]> {
  const rows = await api.get<TurnSummaryDTO[]>(
    `/conversations/${encodeURIComponent(conversationId)}/traces`,
  );
  return rows.map(toTurn);
}

/** A run with nothing saved yet (a turn that just started) is an empty trace. */
export async function fetchRunTrace(runId: string): Promise<TraceSpan[]> {
  try {
    const rows = await api.get<SpanFields[]>(
      `/runs/${encodeURIComponent(runId)}/trace`,
    );
    return rows.map(toSpan);
  } catch (err) {
    if (isNotFound(err)) return [];
    throw err;
  }
}

export async function fetchBackground(
  days: number,
): Promise<BackgroundActivity> {
  return toBackground(
    await api.get<BackgroundActivityDTO>(`/traces/background?days=${days}`),
  );
}

function isNotFound(err: unknown): boolean {
  return isApiError(err) && err.status === 404;
}
