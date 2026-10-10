/**
 * The thread list's vocabulary, and the composer's readout.
 *
 * Pure DTO→seam translations for the things that describe a *conversation* rather than
 * anything inside one: its active run, its metrics, its row in the rail. Nothing here
 * touches a store, a resource or the network.
 */

import { sessionMode } from "~/lib/modes";
import type { ActiveRun, ChatSummary, ConversationStats } from "../model";
import type {
  ActiveRunDTO,
  ConversationSummaryDTO,
  RunMetricsDTO,
} from "./wire";

export function toActiveRun(
  dto: ActiveRunDTO | null | undefined,
): ActiveRun | null {
  return dto
    ? {
        id: dto.id,
        kind: dto.kind,
        status: dto.status,
        lastSeq: dto.last_seq,
        promptMessageId: dto.prompt_message_id ?? null,
      }
    : null;
}

/** The composer's readout, from the backend's metrics payload.
 *
 *  One mapper for both sources on purpose: the live `run.metrics` frame and the
 *  conversation load's `stats` are the *same* shape server-side, so mapping them in
 *  one place is what guarantees a reload can't quietly report something different
 *  from what the stream reported a moment earlier.
 *
 *  A pure rename — no arithmetic. The averages, the rate and the ratio all arrive
 *  derived, because deriving them here would mean two implementations of the same
 *  formula and a second answer to a question the backend already answered. Nulls
 *  pass through untouched: they mean unmeasured, and coercing one to 0 would turn
 *  "nobody reported this" into a measurement. */
export function toStats(dto: RunMetricsDTO): ConversationStats {
  return {
    turns: dto.turns,
    steps: dto.steps,
    toolCalls: dto.tool_calls,
    inputTokens: dto.input_tokens,
    outputTokens: dto.output_tokens,
    cacheHitRatio: dto.cache_hit_ratio,
    llmMs: dto.llm_ms,
    toolMs: dto.tool_ms,
    ttftAvgMs: dto.ttft_avg_ms,
    tokensPerSecond: dto.output_tokens_per_second,
    lastRequest: dto.last_request
      ? {
          route: dto.last_request.route,
          inputTokens: dto.last_request.input_tokens,
          outputTokens: dto.last_request.output_tokens,
          cacheReadTokens: dto.last_request.cache_read_tokens,
          cacheWriteTokens: dto.last_request.cache_write_tokens,
        }
      : null,
  };
}

/** A readable one-line title for a thread that the operator hasn't named. */
export function deriveTitle(dto: ConversationSummaryDTO): string {
  if (dto.title) return dto.title;
  if (dto.preview) return dto.preview.slice(0, 60);
  return "Untitled conversation";
}

export function toSummary(dto: ConversationSummaryDTO): ChatSummary {
  return {
    id: dto.id,
    title: deriveTitle(dto),
    updatedAt: dto.updated_at,
    createdAt: dto.created_at,
    messageCount: dto.message_count,
    preview: dto.preview ?? undefined,
    workSummary: dto.work_summary ?? undefined,
    model: dto.model ?? undefined,
    activity: dto.activity ?? undefined,
    lastOutcome: dto.last_outcome ?? undefined,
    mode: sessionMode(dto.mode ?? undefined),
    workspace: dto.workspace ?? undefined,
    projectId: dto.project_id ?? undefined,
  };
}
