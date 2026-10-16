/**
 * A worktree thread's branch, and what the thread changed in the operator's own project
 * folders: fetched once each for everyone who shows them, the header's chips and the
 * Changes surface, so two views cannot disagree while one is in flight.
 *
 * Both reads degrade to `null` rather than erroring: a thread with no worktree, or no
 * changes, is the ordinary case, and its transcript must not become unreadable when the
 * answer is 404.
 */

import { createMemo } from "solid-js";
import { branchWanted } from "./branchWanted";
import {
  fetchBranch,
  fetchChanges,
  type BranchState,
  type ThreadChanges,
} from "./data";
import { createThreadRead, type ThreadRead } from "./threadRead";

export interface ThreadChangesApi {
  changes: ThreadRead<ThreadChanges | null>;
  branch: ThreadRead<BranchState | null>;
}

export function createThreadChanges(
  conversationId: () => string | null,
  revision: () => number,
): ThreadChangesApi {
  const changes = createThreadRead(conversationId, revision, fetchChanges);
  // Only a thread with a worktree owns a branch to ask about; asking for any other is a
  // request the backend can only answer 404. The changes read says which this is, so a
  // thread switch asks nothing until the new thread's answer lands.
  const wanted = createMemo(() =>
    branchWanted(conversationId(), changes.latest()?.worktree),
  );
  const branch = createThreadRead(wanted, revision, fetchBranch);
  return { changes, branch };
}
