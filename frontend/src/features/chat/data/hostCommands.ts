/**
 * A command the agent ran, read off whatever its tool returned.
 *
 * Both command tools — the sandboxed host command and the worktree shell — answer with
 * a structured record when they actually execute, and with a plain sentence when a guard
 * refused them. Everything here keys off that distinction rather than off a tool name,
 * so a command tool the client has never heard of arrives already understood.
 *
 * Pure: a DTO (or the same shape off a stream event) in, a seam type out. One function
 * serves the cold conversation load and the live SSE fold, which is what guarantees an
 * exit code looks identical whether the operator watched it arrive or reloaded into it.
 */

import type {
  CommandBoundaryFacts,
  HostCommand,
  HostCommandPhase,
} from "../model";
import { NARRATION_ARG } from "../toolSummary";
import type { HostResult, ToolCallDTO } from "./wire";

/** Why this command was run, in the agent's words.
 *
 *  Two arguments can carry it and which one depends on the tool. `code_run_host_command`
 *  declares an `explanation` of its own, because that sentence is *approval copy* — the
 *  operator decides on it, so it is part of the call rather than decoration. The worktree
 *  shell declares no such thing and relies on the `narration` every tool is offered
 *  (`tools/narration.py`).
 *
 *  Reading only the first is what this used to do, and it meant the worktree shell —
 *  the one whose terminal the operator watches most — was the one place the narration never
 *  appeared. The approval copy still wins where both exist: it is the sentence the operator
 *  was actually shown when they said yes. */
export function commandReason(
  args: Record<string, unknown>,
): string | undefined {
  for (const key of ["explanation", NARRATION_ARG]) {
    const value = args[key];
    if (typeof value === "string" && value.trim()) return value;
  }
  return undefined;
}

/** Pull the structured streams out of a host command's result, or null when the
 *  payload isn't that shape (e.g. a denial string) — callers leave the phase
 *  untouched in that case so a denied command stays denied. */
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

/** The declaration and the fence's verdict off any result that carries them, or
 *  `undefined` for the overwhelming majority that do not.
 *
 *  Keyed on the presence of `reach` rather than on a tool name, because it is the tools
 *  that *run a command* that carry these and the client should not hold a second list of
 *  which those are — the backend attaches the pair to every executing command tool, and
 *  a new one would arrive here already understood. */
export function commandBoundary(
  result: unknown,
): CommandBoundaryFacts | undefined {
  const r = parseHostResult(result);
  return r ? boundaryOf(r) : undefined;
}

function boundaryOf(r: HostResult): CommandBoundaryFacts | undefined {
  if (!r.reach) return undefined;
  return {
    reach: r.reach,
    fenced: r.fenced,
    unfencedReason: r.unfenced_reason,
    fenceNote: r.fence_note,
  };
}

/**
 * A finished terminal's outcome, from whatever its tool returned — `null` when the
 * result carries no outcome at all.
 *
 * **An object means it executed; a string means it did not.** Both command tools
 * answer with a record when they actually run, and a guard refusal — a tool the thread
 * cannot use, a host that cannot fence — comes back as a plain sentence the model was handed.
 * That is the whole test, and it is one test rather than two because the worktree
 * shell stopped composing its own labelled transcript: it reports the streams, the
 * exit code, the duration and its fence verdict as fields, the same way the sandboxed
 * host command always has. The client used to pull that transcript back apart with a
 * parser that read a format the shell harness owned — see `toHostCommand` for what
 * the string now means instead.
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

/** Map a persisted terminal tool call (cold history) to the terminal model.
 *  A stored call has already run, so its phase comes from the recorded status. */
export function toHostCommand(dto: ToolCallDTO): HostCommand {
  const outcome = toTerminalOutcome(dto.result);
  // A command tool always returns a structured dict when it actually executes, so a
  // plain-string result means it never ran — it was refused, and the string is the
  // refusal the model was handed. Surface that instead of a green OK.
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
    unfencedReason: outcome?.unfencedReason,
    fenceNote: outcome?.fenceNote,
    // Carry whatever diagnostic exists: the result hint, the denial message, or a
    // retry/validation error projected onto the tool call.
    error: outcome?.error ?? denial ?? dto.error ?? undefined,
  };
}
