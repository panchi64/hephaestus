import { Show, type JSX } from "solid-js";
import { Row, Text, Tooltip } from "~/ui";
import type { ProjectRepo } from "../model";

/** A project's git state in one line.
 *
 *  The uncommitted count is the important half and the reason this component exists
 *  rather than a bare "git ✓". A worktree branches from the project's base ref, so
 *  **uncommitted work in the operator's own checkout is invisible to the agent there**.
 *  That is the price of never touching their tree, and it should be readable before
 *  they start a session rather than discovered halfway through one. */
export function RepoStatus(props: { repo: ProjectRepo }): JSX.Element {
  return (
    <Row gap={3} class="items-center">
      <Show
        when={props.repo.exists}
        fallback={
          <Text variant="micro" tone="alert">
            Path missing
          </Text>
        }
      >
        <Show
          when={props.repo.isGitRepo}
          fallback={
            <Tooltip label="A worktree needs a repository — it will offer to create one">
              <Text variant="micro" tone="warn">
                Not a repo
              </Text>
            </Tooltip>
          }
        >
          <Show when={props.repo.currentBranch}>
            {(branch) => (
              <Text variant="micro" tone="dim">
                {branch()}
              </Text>
            )}
          </Show>
          <Show
            when={(props.repo.uncommittedChanges ?? 0) > 0}
            fallback={
              <Text variant="micro" tone="nominal">
                Clean
              </Text>
            }
          >
            <Tooltip label="Uncommitted changes are not visible to the agent — its worktree branches from the base ref">
              <Text variant="micro" tone="warn">
                {props.repo.uncommittedChanges} UNCOMMITTED
              </Text>
            </Tooltip>
          </Show>
        </Show>
      </Show>
    </Row>
  );
}
