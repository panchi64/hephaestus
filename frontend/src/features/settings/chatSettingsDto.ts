/**
 * The chat preferences on the wire, and the two mappings to and from them.
 *
 * Split out of `data.ts` for one reason: this is pure data → data, and `data.ts` is a
 * module that reaches the network and the models store the moment it is imported. Only
 * one of those two things can be pinned by a test in a runtime with no DOM, and it is
 * this one — which happens to be the half where a mistake is silent.
 *
 * **The silent mistake is the falsy value.** A PUT here carries only the keys the
 * caller touched, and "touched" has to be an `!== undefined` test rather than a
 * truthiness one, because two of these settings have a meaningful falsy value: a **null**
 * wall clock or **null** sub-agent cap is the value that removes the bound. Under a
 * truthiness test both are dropped on the way out, with no error and no sign — and only
 * for the operators who chose them.
 */

import type { ChatSettings } from "./model";

export interface ChatSettingsDTO {
  auto_compact_enabled: boolean;
  auto_compact_threshold: number;
  work_summary_idle_minutes: number;
  context_warn_threshold: number;
  context_alert_threshold: number;
  first_token_timeout_s: number | null;
  inactivity_timeout_s: number;
  wall_clock_timeout_s: number | null;
  subagent_max_concurrent: number | null;
}

/** The single snake_case→camel mapper for the stored chat preferences. */
export function toChatSettings(dto: ChatSettingsDTO): ChatSettings {
  return {
    autoCompactEnabled: dto.auto_compact_enabled,
    autoCompactThreshold: dto.auto_compact_threshold,
    workSummaryIdleMinutes: dto.work_summary_idle_minutes,
    contextWarnThreshold: dto.context_warn_threshold,
    contextAlertThreshold: dto.context_alert_threshold,
    firstTokenTimeoutS: dto.first_token_timeout_s,
    inactivityTimeoutS: dto.inactivity_timeout_s,
    wallClockTimeoutS: dto.wall_clock_timeout_s,
    subagentMaxConcurrent: dto.subagent_max_concurrent,
  };
}

/** Map a camelCase patch to the backend's snake_case body (an omitted field is left
 *  unchanged on the backend, so only present keys are written). */
export function toChatSettingsBody(
  patch: Partial<ChatSettings>,
): Record<string, unknown> {
  const body: Record<string, unknown> = {};
  if (patch.autoCompactEnabled !== undefined)
    body.auto_compact_enabled = patch.autoCompactEnabled;
  if (patch.autoCompactThreshold !== undefined)
    body.auto_compact_threshold = patch.autoCompactThreshold;
  if (patch.workSummaryIdleMinutes !== undefined)
    body.work_summary_idle_minutes = patch.workSummaryIdleMinutes;
  if (patch.contextWarnThreshold !== undefined)
    body.context_warn_threshold = patch.contextWarnThreshold;
  if (patch.contextAlertThreshold !== undefined)
    body.context_alert_threshold = patch.contextAlertThreshold;
  if (
    patch.firstTokenTimeoutS !== undefined &&
    patch.firstTokenTimeoutS !== null
  )
    body.first_token_timeout_s = patch.firstTokenTimeoutS;
  if (patch.inactivityTimeoutS !== undefined)
    body.inactivity_timeout_s = patch.inactivityTimeoutS;
  if (patch.wallClockTimeoutS !== undefined)
    body.wall_clock_timeout_s = patch.wallClockTimeoutS;
  if (patch.subagentMaxConcurrent !== undefined)
    body.subagent_max_concurrent = patch.subagentMaxConcurrent;
  return body;
}
