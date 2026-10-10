import { sessionModeSpec, type SessionMode } from "~/lib/modes";

/** The thread to ask a branch of, or null where there is nothing to ask. Only a thread
 *  *loaded* in a worktree mode owns one; the mode must be that thread's own, not the
 *  window's, or a switch from a code thread would ask about the sandbox thread opened
 *  next. Its own module so the rule is testable without the data seam behind it. */
export function branchWanted(
  id: string | null,
  loaded: { id: string; mode: SessionMode } | null,
): string | null {
  return id !== null &&
    loaded?.id === id &&
    sessionModeSpec(loaded.mode).workspace === "worktree"
    ? id
    : null;
}
