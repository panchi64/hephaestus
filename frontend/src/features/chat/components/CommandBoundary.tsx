import { Show, createSignal, type JSX } from "solid-js";
import { Button, Row, Stack, Text, toast } from "~/ui";
import { allowableHost, boundaryReadout } from "../commandBoundary";
import { useAllowHost } from "../allowHostContext";
import type { CommandBoundaryFacts } from "../model";

/**
 * What a command said it needed, and what actually held it to that.
 *
 * Rendered as a pair because the two can disagree: `reach` is the model's declaration,
 * checked against every path the command names before it runs; `fenced` is whether an OS
 * fence built from it was applied. A command can declare the workspace and still run
 * unfenced (a host with no sandbox primitive), so showing one alone would present a
 * claim as enforcement or vice versa.
 *
 * Both arrive on the result at every permission level; only Auto produces a review row.
 * Renders nothing when neither is present (never executed, or an older thread); there is
 * no UNKNOWN state. The host-reach command tool reports the fence verdict without a
 * `reach` and shows it alone.
 *
 * Takes the facts, not a command, so it serves both a terminal and the tool card a
 * backgrounded command renders as.
 */
export function CommandBoundary(props: {
  command: CommandBoundaryFacts;
}): JSX.Element {
  const readout = () => boundaryReadout(props.command);
  return (
    <Show when={readout()}>
      {(r) => (
        <Row gap={2} align="center" class="flex-wrap">
          {/* `meta` rather than `plate`: this reports a state, it doesn't name a region. */}
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
            {/* Warn, not alert: an unfenced command is a weaker guarantee, not a failure. */}
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
 * The fence explaining a failure it caused: `Operation not permitted` on a write outside
 * the worktree reads as a broken command but is the fence working. The backend emits the
 * note only when it recognises that case.
 *
 * When the note names a host a VM command was refused, the operator can allow it for the
 * thread from here. A host-reach refusal names no host and gets no button, since allowing
 * a domain widens the VM's egress, which a command on the operator's machine never used.
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
  const allowHost = useAllowHost();
  const [state, setState] = createSignal<AllowState>("idle");

  async function allow(): Promise<void> {
    if (!allowHost || state() !== "idle") return;
    setState("pending");
    try {
      await allowHost(props.host);
      setState("allowed");
    } catch (err) {
      setState("idle");
      toast.error(
        (err as { detail?: string })?.detail ?? "Unable to allow that host.",
      );
    }
  }

  return (
    <Show when={allowHost}>
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
