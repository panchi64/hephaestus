import { createContext, useContext, type Accessor } from "solid-js";

/** Why a control that rewrites the thread is off while a turn is running. */
export const TURN_LOCK_HINT = "Stop the turn first";

/**
 * Whether a turn is live, for every control that edits, branches, rewinds or deletes the
 * recorded thread.
 *
 * Carries the same fact the stream's own ops guard on (`sending`), so a control and the
 * op behind it cannot disagree about whether the click will do anything.
 */
export const TurnLockContext = createContext<Accessor<boolean>>(() => false);

export const useTurnLocked = (): Accessor<boolean> =>
  useContext(TurnLockContext);
