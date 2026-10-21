import type { RunEvent } from "~/lib/stream";
import type { Approval } from "./model";

export type ApprovalEvent = Extract<RunEvent, { type: "approval.required" }>;

/** The backend fills `explanation` with the guard's reason when the tool wrote none; the
 *  card prints the guard on a line of its own, so the copy is dropped. */
export const explanationOf = (ev: ApprovalEvent): string | undefined =>
  ev.explanation && ev.explanation !== ev.guard ? ev.explanation : undefined;

/** The card for a parked call. `args` arrives as untrusted JSON typed as always present,
 *  so it is defaulted here once. */
export function approvalFromEvent(ev: ApprovalEvent): Approval {
  return {
    toolCallId: ev.tool_call_id,
    name: ev.name,
    args: ev.args ?? {},
    summary: ev.summary,
    explanation: explanationOf(ev),
    guard: ev.guard ?? undefined,
    unconfined: ev.unconfined ?? undefined,
  };
}
