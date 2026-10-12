import type { WorkspaceKind } from "./model";

/** The thread to ask a branch of, or null where there is nothing to ask. Only a thread
 *  in a worktree owns one, and `kind` is that thread's own current answer (undefined
 *  until the listing has it), not the window's. Its own module so the rule is testable
 *  without the data seam behind it. */
export function branchWanted(
  id: string | null,
  kind: WorkspaceKind | undefined,
): string | null {
  return id !== null && kind === "worktree" ? id : null;
}
