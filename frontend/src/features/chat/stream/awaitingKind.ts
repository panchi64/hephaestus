import type { ActionTone } from "~/ui";

/** What the open room is waiting on the operator for, or null. A question outranks
 *  approvals sharing its park, as the backend's `awaiting_kind` does; a stale park has
 *  already resumed elsewhere and waits on nothing. */
export function awaitingKindOf(
  park: { questions: readonly unknown[]; stale: boolean } | null,
  hostCommandPending: boolean,
): ActionTone | null {
  if (park) {
    if (park.stale) return null;
    return park.questions.length > 0 ? "question" : "approval";
  }
  return hostCommandPending ? "approval" : null;
}
