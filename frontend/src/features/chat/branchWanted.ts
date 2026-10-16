/** The thread to ask a branch of, or null where there is nothing to ask. Only a thread
 *  with a worktree owns one, and `worktree` is that thread's own current answer
 *  (undefined until it is read), not the window's. Its own module so the rule is
 *  testable without the data seam behind it. */
export function branchWanted(
  id: string | null,
  worktree: boolean | undefined,
): string | null {
  return id !== null && worktree === true ? id : null;
}
