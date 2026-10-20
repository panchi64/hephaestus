import { Show, type JSX } from "solid-js";
import { Row, Text } from "~/ui";
import type { CommandBoundaryFacts } from "../model";

/**
 * What a command said it needed, and what actually held it to that.
 *
 * Two facts, and the reason they are rendered as a pair rather than as one verdict is
 * that they can disagree. `reach` is the model's own **declaration**, made on the call
 * and checked against every path the command names before anything runs; `fenced` is
 * whether an OS fence built from that declaration was applied to the process. A command
 * can declare the workspace and still run unfenced — a host with no sandbox primitive
 * has none to build — and an operator who only ever saw one of the two would be reading
 * a claim as if it were an enforcement, or an enforcement without knowing what it was
 * enforcing.
 *
 * They arrive on the **result**, at every permission level. That is what makes this a
 * per-command readout rather than a line on a review row: only the Auto level produces a
 * review, so a thread at Manual or Yolo used to run commands with neither fact anywhere
 * on screen.
 *
 * Renders nothing at all when there is no declaration — a call that never executed, or a
 * thread from before the tool carried one. An absent fact says nothing, and a placeholder
 * reading UNKNOWN would be a third state the backend never reports.
 *
 * It takes the **facts**, not a command, so the one component serves both kinds of row
 * that can hold them: a terminal, and the ordinary tool card a backgrounded command
 * renders as.
 */
export function CommandBoundary(props: {
  command: CommandBoundaryFacts;
}): JSX.Element {
  const c = () => props.command;
  return (
    <Show when={c().reach}>
      {(reach) => (
        <Row gap={2} align="center" class="flex-wrap">
          {/* `meta` rather than `plate`: this reports a state, it does not name a
              region. */}
          <Text variant="meta" tone="dim">
            Reach {reach()}
          </Text>
          <Show when={reach() === "offline" && c().network}>
            <Text variant="meta" tone="dim">
              Ran with network
            </Text>
          </Show>
          <Show
            when={c().fenced === false}
            fallback={
              <Show when={c().fenced}>
                <Text variant="meta" tone="dim">
                  Fenced
                </Text>
              </Show>
            }
          >
            {/* The word carries it, not a colour (§12) — and it is warn rather than
                alert because an unfenced command is a weaker guarantee, not a failure.
                The reason rides beside it in sentence form, since "unfenced" alone
                leaves the operator with nothing to do about it. */}
            <Text variant="meta" tone="warn">
              Unfenced
            </Text>
            <Show when={c().unfencedReason}>
              <Text variant="micro" tone="dim" class="min-w-0 break-words">
                {c().unfencedReason}
              </Text>
            </Show>
          </Show>
        </Row>
      )}
    </Show>
  );
}

/**
 * The fence explaining a failure it caused.
 *
 * A permission error in a command's output is the one case where the interesting fact
 * is not in the output: `Operation not permitted` on a write outside the worktree reads
 * as a broken command, and it is the fence doing exactly its job. The backend emits the
 * note only when it recognises that case, so this renders only when there is something
 * to say.
 */
export function FenceNote(props: {
  command: CommandBoundaryFacts;
}): JSX.Element {
  return (
    <Show when={props.command.fenceNote}>
      <Text variant="micro" tone="warn" class="break-words">
        {props.command.fenceNote}
      </Text>
    </Show>
  );
}
