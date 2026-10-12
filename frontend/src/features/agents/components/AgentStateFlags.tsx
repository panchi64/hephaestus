import { Show, type JSX } from "solid-js";
import { StatusFlag, Text } from "~/ui";
import {
  agentState,
  agentStateFlag,
  agentStateLabel,
  isAwaitingPublish,
  type Agent,
} from "../model";

/** The agent's published / draft state, a SHADOWED marker when a published
 *  agent's name resolves to a higher-precedence one, and — where `awaiting` is
 *  set — the plain statement that an agent-authored draft is waiting on the
 *  operator. */
export function AgentStateFlags(props: {
  agent: Agent;
  awaiting?: boolean;
}): JSX.Element {
  const state = () => agentState(props.agent);
  return (
    <>
      <Show when={props.awaiting && isAwaitingPublish(props.agent)}>
        <StatusFlag status="info">AWAITING PUBLISH</StatusFlag>
      </Show>
      <StatusFlag
        status={agentStateFlag(props.agent.published ? "published" : "draft")}
      >
        {agentStateLabel(props.agent.published ? "published" : "draft")}
      </StatusFlag>
      <Show when={state() === "shadowed"}>
        <Text variant="micro" tone="dim">
          SHADOWED
        </Text>
      </Show>
    </>
  );
}
