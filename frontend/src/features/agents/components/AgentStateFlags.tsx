import { Show, type JSX } from "solid-js";
import { StatusFlag, Text } from "~/ui";
import {
  agentState,
  agentStateFlag,
  agentStateLabel,
  isAwaitingPublish,
  type Agent,
} from "../model";

/** The agent's published / draft / shadowed state, and for an agent-authored
 *  draft the plain statement that it is waiting on the operator. */
export function AgentStateFlags(props: { agent: Agent }): JSX.Element {
  const state = () => agentState(props.agent);
  return (
    <>
      <Show when={isAwaitingPublish(props.agent)}>
        <StatusFlag status="info">AWAITING PUBLISH</StatusFlag>
      </Show>
      <Show
        when={state() === "shadowed"}
        fallback={
          <StatusFlag status={agentStateFlag(state())}>
            {agentStateLabel(state())}
          </StatusFlag>
        }
      >
        <Text variant="micro" tone="dim">
          SHADOWED
        </Text>
      </Show>
    </>
  );
}
