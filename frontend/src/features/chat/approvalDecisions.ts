import type { Approval, ApprovalDecision } from "./model";

/** What the operator said to one call: the verdict, and what kind of no it was. */
export interface Answer {
  approved: boolean;
  /** Only meaningful on a no — see `ApprovalDecision.intent`. */
  intent: "deny" | "revise";
}

/** The keys that answer a lone call without the pointer. Printed on the buttons and
 *  bound by the host, so the hint and the binding cannot drift apart. */
export const ONE_CLICK_KEYS = { approve: "y", deny: "n" } as const;

/** Whether a park is a single yes-or-no: exactly one pending call and nothing else to
 *  answer. Only then is picking a verdict and pressing submit one gesture too many — a
 *  park holding several calls (or a question beside the call) still collects every
 *  verdict before the one body goes out. */
export function isOneClickPark(
  approvalCount: number,
  questionCount: number,
): boolean {
  return approvalCount === 1 && questionCount === 0;
}

/** Whether this answer, given to the lone call, is the whole decision. Asking for
 *  changes is the one answer that still needs words, so it waits for the note. */
export function settlesImmediately(answer: Answer): boolean {
  return answer.approved || answer.intent === "deny";
}

/** The wire decisions for the calls answered so far. `scopeOf` is the grant opt-in for a
 *  call at the moment it is answered — the checkbox rides the same submission. */
export function decisionsFor(
  approvals: Approval[],
  given: Record<string, Answer>,
  scopeOf: (approval: Approval, approved: boolean) => ApprovalDecision["scope"],
): ApprovalDecision[] {
  return approvals
    .filter((a) => a.toolCallId in given)
    .map((a) => {
      const answer = given[a.toolCallId];
      return {
        tool_call_id: a.toolCallId,
        approved: answer.approved,
        scope: scopeOf(a, answer.approved),
        // Only travels on a no, and the backend ignores it on a yes — but sending an
        // intent with an approval would still be saying something that is not true
        // of it.
        ...(answer.approved ? {} : { intent: answer.intent }),
      };
    });
}
