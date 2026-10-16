import { Show, type JSX } from "solid-js";
import { Text } from "~/ui";
import type { Subagent } from "../data";

/** `BRAX · Tripodes`. Renders nothing for a sub-agent with no callsign, so a caller
 *  that always places it beside the handle falls back to the handle alone. */
export function SubagentDesignation(props: {
  subagent: Pick<Subagent, "callsign" | "agentClass">;
}): JSX.Element {
  return (
    <Show when={props.subagent.callsign}>
      {(callsign) => (
        <span class="inline-flex shrink-0 items-baseline gap-1">
          <Text variant="label" tone="bright">
            {callsign()}
          </Text>
          <Show when={props.subagent.agentClass}>
            {(agentClass) => (
              <>
                <Text variant="label" tone="dim">
                  ·
                </Text>
                <Text variant="label" tone="dim">
                  {agentClass()}
                </Text>
              </>
            )}
          </Show>
        </span>
      )}
    </Show>
  );
}
