/**
 * Whose task list is whose, when a thread is not the only one working.
 *
 * A sub-agent is its own conversation, so the list it keeps for itself is its own row in
 * its own thread — nothing merges the two, and until now nothing showed the operator that
 * a delegated piece of work had a shape at all. The Tasks surface would say "3/4" while
 * four sub-agents were quietly working through lists of their own.
 *
 * **Grouped rather than merged.** Interleaving them into one list would be a list nobody
 * could act on: two agents' steps in one sequence read as one plan whose order makes no
 * sense, and a `2/7` over the top of it counts work being done in parallel as though it
 * were being done in a line. A heading per sub-agent keeps each list the thing it is.
 *
 * **The thread's own list comes first and carries no heading.** It is the answer to
 * "what is being done here", and labelling it would make it look like one delegate among
 * several rather than the work the operator asked for.
 *
 * Pure, and it takes the delegates structurally rather than as `Subagent`s: what it needs
 * is a name and a list, and a rule about how a list is read should not depend on the
 * shape of a card.
 */

import type { TaskItem } from "~/lib/stream/events";
import { designationText } from "./data/subagents";

export interface TaskGroup {
  /** The sub-agent whose list this is, or `null` for the thread's own. */
  label: string | null;
  items: TaskItem[];
}

/** What a delegate has to be for its list to be grouped: a name to file it under, and
 *  the list itself. */
export interface TaskDelegate {
  handle: string;
  callsign?: string;
  agentClass?: string;
  tasks: TaskItem[];
}

/**
 * The thread's own tasks, then one group per sub-agent that has written any.
 *
 * A sub-agent with an empty list is left out entirely rather than shown as an empty
 * group — "has not written a list" and "has nothing to do" are the same state to the
 * operator, and a heading over nothing is a row that only asks a question.
 *
 * Delegates keep the order they arrive in, which is the order their cards are in.
 */
export function groupTasks(
  own: readonly TaskItem[],
  delegates: readonly TaskDelegate[],
): TaskGroup[] {
  const groups: TaskGroup[] = [];
  if (own.length > 0) groups.push({ label: null, items: [...own] });
  for (const delegate of delegates)
    if (delegate.tasks.length > 0)
      groups.push({
        label: [designationText(delegate), delegate.handle]
          .filter(Boolean)
          .join(" · "),
        items: [...delegate.tasks],
      });
  return groups;
}

/** Every task on screen, whoever wrote it — what the surface's done/total counts.
 *
 *  Counted across the groups rather than per group: the question the readout answers is
 *  "how much of this is left", and a thread that delegated everything would otherwise
 *  read `0/0` above four lists in progress. */
export function flattenGroups(groups: readonly TaskGroup[]): TaskItem[] {
  return groups.flatMap((g) => g.items);
}
