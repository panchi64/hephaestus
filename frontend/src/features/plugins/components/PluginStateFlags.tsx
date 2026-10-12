import { Show, type JSX } from "solid-js";
import { StatusFlag } from "~/ui";
import {
  hasPendingDraft,
  isAgentDrafted,
  pluginState,
  pluginStateFlag,
  pluginStateLabel,
  type Plugin,
} from "../model";

/** Published / disabled / draft, plus the two facts the operator acts on: a
 *  draft is waiting on review, and an agent wrote it. */
export function PluginStateFlags(props: { plugin: Plugin }): JSX.Element {
  const state = () => pluginState(props.plugin);
  return (
    <>
      <StatusFlag status={pluginStateFlag(state())}>
        {pluginStateLabel(state())}
      </StatusFlag>
      <Show when={hasPendingDraft(props.plugin) && state() !== "draft"}>
        <StatusFlag status="info">DRAFT AWAITING REVIEW</StatusFlag>
      </Show>
      <Show when={isAgentDrafted(props.plugin)}>
        <StatusFlag status="idle">DRAFTED BY AN AGENT</StatusFlag>
      </Show>
    </>
  );
}
