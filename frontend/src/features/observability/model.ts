/** Observability data contracts: traced spans, per-turn rollups and background work.
 *  Every token and cost field is null when nothing was reported or priced, never 0. */

import { parseInstant } from "~/lib/format";
import type { SpanKind } from "~/lib/stream/events";

export type { SpanKind };

/** One finished agent run, model request or tool call. */
export interface TraceSpan {
  spanId: string;
  traceId: string;
  parentSpanId: string | null;
  kind: SpanKind;
  agentName: string;
  model: string | null;
  toolName: string | null;
  startedAt: string;
  durationMs: number;
  ttftMs: number | null;
  inputTokens: number | null;
  outputTokens: number | null;
  cacheReadTokens: number | null;
  cacheWriteTokens: number | null;
  costUsd: number | null;
  errorType: string | null;
}

/** One run of a conversation, rolled up by the backend. */
export interface TurnSummary {
  runId: string;
  startedAt: string;
  durationMs: number;
  modelCalls: number;
  toolCalls: number;
  inputTokens: number | null;
  outputTokens: number | null;
  /** Everything the run paid for, and the share the chat agent spent itself; the rest
   *  is background work the turn started (its title, a page distill). */
  costUsd: number | null;
  chatCostUsd: number | null;
  errors: number;
}

export interface AgentActivity {
  agentName: string;
  calls: number;
  costUsd: number | null;
  avgDurationMs: number;
  lastRunAt: string;
}

export interface DayCost {
  day: string;
  costUsd: number;
}

export interface BackgroundCall {
  spanId: string;
  agentName: string;
  model: string | null;
  /** Null for work no thread started, and for a private thread's while the area is locked. */
  conversationId: string | null;
  startedAt: string;
  durationMs: number;
  costUsd: number | null;
  errorType: string | null;
}

/** Work by every agent other than the chat agent and its sub-agents. */
export interface BackgroundActivity {
  days: number;
  agents: AgentActivity[];
  perDay: DayCost[];
  recent: BackgroundCall[];
}

/** The turn running when `at` happened: the latest to start at or before it. A thread
 *  runs one turn at a time, so turns never overlap and this is exact. */
export function turnAt(turns: TurnSummary[], at: string): TurnSummary | null {
  const moment = parseInstant(at);
  if (Number.isNaN(moment)) return null;
  let found: TurnSummary | null = null;
  for (const turn of turns) {
    const start = parseInstant(turn.startedAt);
    if (start <= moment && (!found || start >= parseInstant(found.startedAt))) {
      found = turn;
    }
  }
  return found;
}

/** A traced agent's name as the operator reads it. */
export function agentLabel(name: string): string {
  return AGENT_LABELS[name] ?? name.replaceAll("_", " ");
}

const AGENT_LABELS: Record<string, string> = {
  main: "chat",
  subagent: "sub-agent",
  title: "titler",
  work_summary: "work summary",
  judge: "judge",
  vision: "vision OCR",
  mail_triage: "mail triage",
  mail_style: "mail style",
  page_distill: "page distill",
  calendar_parse: "calendar parse",
  routine_parse: "routine parse",
  eval_judge: "eval judge",
};
