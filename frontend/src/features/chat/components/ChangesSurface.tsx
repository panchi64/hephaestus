import { Show, type JSX } from "solid-js";
import type { BranchState, ThreadChanges } from "../data";
import { DiffSurface } from "./DiffSurface";
import { ProjectChangesList } from "./ProjectChangesList";

/**
 * Everything the thread changed: the edits it made in the operator's own project
 * folders, which can be put back, and its worktree's branch, which can be merged or
 * discarded. A thread may have either, both or neither.
 */
export function ChangesSurface(props: {
  conversationId: () => string | null;
  changes: () => ThreadChanges | null | undefined;
  branch: () => BranchState | null | undefined;
  onChanged: () => void;
  fontStep?: number;
  softWrap?: boolean;
}): JSX.Element {
  const projects = () => props.changes()?.projects ?? [];
  return (
    <div class="flex h-full min-h-0 flex-col overflow-auto scrollbar-thin">
      <Show when={projects().length > 0 && props.conversationId()}>
        {(id) => (
          <ProjectChangesList
            conversationId={id()}
            projects={projects()}
            onChanged={props.onChanged}
            fontStep={props.fontStep}
            softWrap={props.softWrap}
          />
        )}
      </Show>
      {/* The branch view also owns the empty state, so it shows when there is nothing
          else to show. */}
      <Show when={props.branch() || projects().length === 0}>
        <div class="min-h-96 flex-1">
          <DiffSurface
            branch={props.branch}
            onChanged={props.onChanged}
            fontStep={props.fontStep}
            softWrap={props.softWrap}
          />
        </div>
      </Show>
    </div>
  );
}
