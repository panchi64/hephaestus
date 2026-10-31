/** Who owns the composer's effort pick, and what a send names.
 *
 *  The pick belongs to the thread it was made on. Opening another thread re-seats the
 *  control from that thread's stored level, and a send names a level only for the thread
 *  that owns the control's value, so a stale pick is never written onto the next thread. */

export interface EffortSeatInput {
  currentId: string | null;
  /** The thread the control's value belongs to; null for the staged new thread. */
  owner: string | null;
  /** The open thread's stored level ("" is none), or undefined while it loads. */
  stored: string | undefined;
}

export interface EffortSeat {
  value: string;
  owner: string | null;
}

/** The value to seat and the thread that then owns it, or null to leave the control be.
 *  While the open thread loads, Default stands in and the owner stays put, so the
 *  stored level still seats once it lands. */
export function seatEffort(input: EffortSeatInput): EffortSeat | null {
  if (input.owner === input.currentId) return null;
  if (input.stored === undefined) return { value: "", owner: input.owner };
  return { value: input.stored, owner: input.currentId };
}

/** What a send names: the control's value when it belongs to this thread, else nothing. */
export function effortToSend(input: {
  choice: string;
  owner: string | null;
  currentId: string | null;
}): string | undefined {
  return input.owner === input.currentId ? input.choice : undefined;
}
