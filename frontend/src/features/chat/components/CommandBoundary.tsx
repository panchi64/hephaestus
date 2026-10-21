import { Show, createSignal, type JSX } from "solid-js";
import { Button, Row, Stack, Text, toast } from "~/ui";
import { allowableHost, boundaryReadout } from "../commandBoundary";
import { useConversationId } from "../conversationContext";
import { allowDomain } from "../data/egress";
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
 * Renders nothing at all when there is neither a declaration nor a fence verdict — a call
 * that never executed, or a thread from before the tools carried them. An absent fact says
 * nothing, and a placeholder reading UNKNOWN would be a third state the backend never
 * reports. The host-reach command tool reports the fence verdict without a `reach`, and
 * shows the verdict alone.
 *
 * It takes the **facts**, not a command, so the one component serves both kinds of row
 * that can hold them: a terminal, and the ordinary tool card a backgrounded command
 * renders as.
 */
export function CommandBoundary(props: {
  command: CommandBoundaryFacts;
}): JSX.Element {
  const readout = () => boundaryReadout(props.command);
  return (
    <Show when={readout()}>
      {(r) => (
        <Row gap={2} align="center" class="flex-wrap">
          {/* `meta` rather than `plate`: this reports a state, it does not name a
              region. */}
          <Show when={r().reach}>
            <Text variant="meta" tone="dim">
              Reach {r().reach}
            </Text>
          </Show>
          <Show when={r().ranWithNetwork}>
            <Text variant="meta" tone="dim">
              Ran with network
            </Text>
          </Show>
          <Show when={r().fence === "fenced"}>
            <Text variant="meta" tone="dim">
              Fenced
            </Text>
          </Show>
          <Show when={r().fence === "unfenced"}>
            {/* The word carries it, not a colour (§12) — and it is warn rather than
                alert because an unfenced command is a weaker guarantee, not a failure.
                The reason rides beside it in sentence form, since "unfenced" alone
                leaves the operator with nothing to do about it. */}
            <Text variant="meta" tone="warn">
              Unfenced
            </Text>
            <Show when={r().unfencedReason}>
              <Text variant="micro" tone="dim" class="min-w-0 break-words">
                {r().unfencedReason}
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
 *
 * When the note names a host a VM command was refused, the operator can allow it for the
 * thread from here. A host-reach refusal names no host and gets no button: allowing a
 * domain widens the VM's egress, which a command on the operator's own machine never used.
 */
export function FenceNote(props: {
  command: CommandBoundaryFacts;
}): JSX.Element {
  return (
    <Show when={props.command.fenceNote}>
      <Stack gap={1} class="items-start">
        <Text variant="micro" tone="warn" class="break-words">
          {props.command.fenceNote}
        </Text>
        <Show when={allowableHost(props.command)}>
          {(host) => <AllowHost host={host()} />}
        </Show>
      </Stack>
    </Show>
  );
}

type AllowState = "idle" | "pending" | "allowed";

function AllowHost(props: { host: string }): JSX.Element {
  const conversationId = useConversationId();
  const [state, setState] = createSignal<AllowState>("idle");

  async function allow(): Promise<void> {
    const id = conversationId();
    if (!id || state() !== "idle") return;
    setState("pending");
    try {
      await allowDomain(id, props.host);
      setState("allowed");
    } catch (err) {
      setState("idle");
      toast.error(
        (err as { detail?: string })?.detail ?? "Unable to allow that host.",
      );
    }
  }

  return (
    <Show when={conversationId()}>
      <Show
        when={state() !== "allowed"}
        fallback={
          <Text variant="micro" tone="dim" class="break-words">
            {props.host} is allowed for this thread. Retry the command and it
            can reach it.
          </Text>
        }
      >
        <Button
          variant="default"
          size="sm"
          leading="link"
          pending={state() === "pending"}
          onClick={() => void allow()}
        >
          {`Allow ${props.host} for this thread`}
        </Button>
      </Show>
    </Show>
  );
}
