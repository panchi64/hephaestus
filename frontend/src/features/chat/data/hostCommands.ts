/**
 * A command the agent ran, read off whatever its tool returned.
 *
 * Both command tools (the sandboxed host command and the worktree shell) return a
 * structured record when they execute and a plain sentence when a guard refused them.
 * Everything here keys off that, not a tool name. The same functions serve the cold load
 * and the live SSE fold, so a result renders identically either way.
 */

import type {
  CommandBoundaryFacts,
  HostCommand,
  HostCommandPhase,
} from "../model";
import { NARRATION_ARG } from "../toolSummary";
import type { HostResult, ToolCallDTO } from "./wire";

/** Why this command was run, in the agent's words. `code_run_host_command` declares an
 *  `explanation` (the approval copy the operator decided on); the worktree shell relies on
 *  the `narration` every tool is offered (`tools/narration.py`). The explanation wins where
 *  both exist. */
export function commandReason(
  args: Record<string, unknown>,
): string | undefined {
  for (const key of ["explanation", NARRATION_ARG]) {
    const value = args[key];
    if (typeof value === "string" && value.trim()) return value;
  }
  return undefined;
}

/** Null when the payload isn't a structured result (e.g. a denial string), so callers
 *  leave the phase untouched and a denied command stays denied. */
export function parseHostResult(result: unknown): HostResult | null {
  if (result == null || typeof result !== "object") return null;
  const r = result as Record<string, unknown>;
  const known =
    typeof r.ok === "boolean" ||
    typeof r.stdout === "string" ||
    typeof r.exit_code === "number" ||
    typeof r.error === "string";
  return known ? (r as HostResult) : null;
}

export function hostPhaseFromResult(r: HostResult): HostCommandPhase {
  return r.ok === false || r.error != null ? "error" : "ok";
}

/** The declared reach and the fence's verdict, or `undefined` for results that carry
 *  neither. Keyed on `reach`/`fenced` rather than a tool name: the backend attaches them
 *  to every executing command tool. */
export function commandBoundary(
  result: unknown,
): CommandBoundaryFacts | undefined {
  const r = parseHostResult(result);
  return r ? boundaryOf(r) : undefined;
}

function boundaryOf(r: HostResult): CommandBoundaryFacts | undefined {
  if (!r.reach && r.fenced === undefined) return undefined;
  return {
    reach: r.reach,
    fenced: r.fenced,
    network: r.network,
    unfencedReason: r.unfenced_reason,
    fenceNote: r.fence_note,
    deniedHost: r.denied_host,
  };
}

/**
 * A finished terminal's outcome, or `null` when the result carries none.
 *
 * An object means it executed; a string means a guard refused it (see `toHostCommand`).
 */
export function toTerminalOutcome(
  result: unknown,
): Partial<HostCommand> | null {
  const r = parseHostResult(result);
  return r
    ? {
        phase: hostPhaseFromResult(r),
        exitCode: r.exit_code,
        stdout: r.stdout,
        stderr: r.stderr,
        timedOut: r.timed_out,
        error: r.error,
        elapsedMs: r.duration_ms,
        ...boundaryOf(r),
      }
    : null;
}

/** Map a persisted terminal tool call to the terminal model; the phase comes from the
 *  recorded status. */
export function toHostCommand(dto: ToolCallDTO): HostCommand {
  const outcome = toTerminalOutcome(dto.result);
  // A plain-string result means the command never ran; the string is the refusal the
  // model was handed.
  const denial =
    !outcome && typeof dto.result === "string" && dto.result
      ? dto.result
      : undefined;
  const phase: HostCommandPhase = denial
    ? "denied"
    : dto.status === "running"
      ? "running"
      : dto.status === "error"
        ? "error"
        : (outcome?.phase ?? "ok");
  return {
    toolCallId: dto.id,
    name: dto.name,
    command: typeof dto.args.command === "string" ? dto.args.command : "",
    explanation: commandReason(dto.args),
    phase,
    exitCode: outcome?.exitCode,
    stdout: outcome?.stdout,
    stderr: outcome?.stderr,
    timedOut: outcome?.timedOut,
    elapsedMs: outcome?.elapsedMs,
    reach: outcome?.reach,
    fenced: outcome?.fenced,
    network: outcome?.network,
    unfencedReason: outcome?.unfencedReason,
    fenceNote: outcome?.fenceNote,
    deniedHost: outcome?.deniedHost,
    error: outcome?.error ?? denial ?? dto.error ?? undefined,
  };
}
