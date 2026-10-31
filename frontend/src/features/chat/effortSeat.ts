/** Who owns the composer's effort pick, and what a send names.
 *
 *  The pick belongs to the thread it was made on. Opening another thread re-seats the
 *  control from that thread's stored level, and a send names a level only for the thread
 *  the operator picked it on, so a stale pick is never written onto the next thread. */

export interface EffortSeatInput {
  currentId: string | null;
  /** The thread the control's value was picked on; null for the staged new thread. */
  owner: string | null;
  /** The open thread's stored level ("" is none), or undefined while it loads. */
  stored: string | undefined;
}

/** The value to seat, or null to leave the operator's own pick in place. */
export function seatEffort(input: EffortSeatInput): string | null {
  if (input.owner === input.currentId) return null;
  return input.stored ?? "";
}

/** What a send names: the pick when it was made on this thread, else nothing. */
export function effortToSend(input: {
  choice: string;
  owner: string | null;
  currentId: string | null;
}): string | undefined {
  return input.owner === input.currentId ? input.choice : undefined;
}
