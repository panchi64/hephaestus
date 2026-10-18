import { createMemo, type JSX } from "solid-js";
import { Select, Tooltip } from "~/ui";
import {
  agentDisplayName,
  DEFAULT_AGENT_NAME,
  pickableAgents,
  type Agent,
} from "~/features/agents/model";

/** The composer status-bar cell choosing which agent the thread runs as from the
 *  next send on. Lists Talos and every published, active agent. */
export function AgentControl(props: {
  agents: readonly Agent[];
  value: string;
  onChange: (name: string) => void;
}): JSX.Element {
  const options = createMemo(() => {
    const list = pickableAgents(props.agents).map((a) => ({
      value: a.name,
      label: agentDisplayName(a.name),
      description: a.description,
    }));
    // A thread's agent that has since been unpublished still reads as itself.
    if (
      props.value !== DEFAULT_AGENT_NAME &&
      !list.some((o) => o.value === props.value)
    ) {
      list.push({ value: props.value, label: props.value, description: "" });
    }
    return list.length
      ? list
      : [
          {
            value: DEFAULT_AGENT_NAME,
            label: agentDisplayName(DEFAULT_AGENT_NAME),
            description: "",
          },
        ];
  });

  return (
    <Tooltip delay={600} side="top" label="Agent this thread runs as">
      <Select
        cell
        value={props.value}
        onChange={props.onChange}
        options={options()}
        aria-label="Agent"
      />
    </Tooltip>
  );
}
