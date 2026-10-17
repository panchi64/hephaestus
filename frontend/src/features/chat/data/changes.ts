/**
 * What a thread changed in the operator's own project folders, and putting it back.
 *
 * A thread that writes a project's folder edits the operator's working copy directly,
 * so the backend keeps each file as it was before the thread's first change. A thread
 * working in a worktree changes its branch instead; `worktree` says it has one, which is
 * what the branch read keys on.
 */

import { api, isApiError } from "~/lib/api";

export interface ChangedFile {
  id: string;
  /** Inside the project. */
  path: string;
  status: "added" | "modified" | "deleted";
  /** Already put back by the operator. */
  reverted: boolean;
  /** False when the original was too large to keep, so it cannot be put back. */
  revertible: boolean;
}

export interface ProjectChanges {
  projectId: string;
  name: string;
  /** The project's folder in the agent's home, `~/projects/<slug>`; "" once deleted. */
  homePath: string;
  files: ChangedFile[];
}

export interface ThreadChanges {
  projects: ProjectChanges[];
  worktree: boolean;
}

export interface RevertReport {
  reverted: string[];
  skipped: { path: string; reason: string }[];
}

/** The thread's changes, or null when the backend has none to report for it. */
export async function fetchChanges(
  conversationId: string,
): Promise<ThreadChanges | null> {
  try {
    return await api.get<ThreadChanges>(
      `/conversations/${conversationId}/changes`,
    );
  } catch (err) {
    if (!isApiError(err) || err.status !== 404) {
      console.warn("changes unavailable", err);
    }
    return null;
  }
}

/** One file, before the thread's first change against it now; null patch for a file
 *  that is not text. */
export async function fetchChangeDiff(
  conversationId: string,
  changeId: string,
): Promise<{ path: string; patch: string | null }> {
  return api.get(`/conversations/${conversationId}/changes/${changeId}/diff`);
}

/** Put files back: the ones named, or every one. */
export async function revertChanges(
  conversationId: string,
  ids?: string[],
): Promise<RevertReport> {
  return api.post<RevertReport>(
    `/conversations/${conversationId}/changes/revert`,
    ids ? { ids } : {},
  );
}

/** The changes still in the operator's folders: listed and not yet put back. */
export function pendingCount(
  changes: ThreadChanges | null | undefined,
): number {
  return (changes?.projects ?? []).reduce(
    (sum, project) => sum + project.files.filter((f) => !f.reverted).length,
    0,
  );
}
