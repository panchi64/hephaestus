/**
 * A worktree thread's branch, fetched once for everyone who wants it.
 *
 * The header's chip owned this resource, which was right while the chip was the only
 * thing that showed a branch. The Diff surface shows the same patch, and two components
 * each holding their own `createResource` over `/worktrees/{id}` means two fetches of the
 * same bytes and — worse — two answers that can disagree for as long as one of them is
 * still in flight.
 *
 * So the room creates it once and hands it to both. Nothing else changes: the read still
 * degrades to `null` rather than erroring, because a thread with no worktree is the
 * ordinary case and its transcript must not become unreadable when the answer is 404.
 */

import { createMemo, createResource } from "solid-js";
import { settled } from "~/lib/resource";
import { branchWanted } from "./branchWanted";
import { fetchBranch, type BranchState } from "./data";
import type { WorkspaceKind } from "./model";

export interface BranchStateApi {
  /** What both consumers should read while a re-fetch is in flight — see below. */
  latest: () => BranchState | null | undefined;
  refetch: () => void;
}

/**
 * `revision` is bumped by the caller when a turn settles, since that is when the agent
 * has just changed something.
 *
 * **`settled`, not the resource's own value.** A plain read goes `undefined` for the
 * length of a refetch, so the chip blinked out of the header and back in at exactly the
 * moment the operator looks up from a finished answer. `settled` holds the previous
 * diffstat until the new one lands, which is also the more honest thing to show: the
 * branch did not stop existing while we asked about it. It also reads `undefined`
 * before the first answer instead of suspending, which a bare `.latest` would do —
 * and the nearest boundary is the whole route.
 *
 * **The held value is tagged with the thread it was read for.** A thread switch is a
 * refetch like any other, and `settled` would hand the previous thread's branch to the
 * new one until its own answer landed. The tag is what makes "hold the last answer" mean
 * *this thread's* last answer.
 */
export function createBranchState(
  conversationId: () => string | null,
  revision: () => number,
  workspaceKind: () => WorkspaceKind | undefined,
): BranchStateApi {
  // Only a thread in a worktree owns a branch to ask about. Asking for a sandbox
  // thread's is a request the backend can only answer 404, three times per open. The
  // kind is the open thread's own current answer, so a thread switch asks nothing until
  // the new thread says what it is, and a thread the agent moves into a worktree starts
  // asking the moment the listing says so.
  const wanted = createMemo(() =>
    branchWanted(conversationId(), workspaceKind()),
  );
  const [branch, { refetch }] = createResource(
    () => {
      const id = wanted();
      return id === null ? undefined : ([id, revision()] as const);
    },
    async ([id]) => ({ id, branch: await fetchBranch(id) }),
  );
  return {
    latest: () => {
      const read = settled(branch);
      return read && read.id === wanted() ? read.branch : undefined;
    },
    refetch: () => {
      if (wanted() !== null) void refetch();
    },
  };
}
