import { createMemo, For, Show, type JSX } from "solid-js";
import { settled } from "~/lib/resource";
import { SettingsPageHeader } from "~/app/settings-dialog/SettingsPageHeader";
import {
  CodeSpanText,
  EmptyState,
  LoadingText,
  Row,
  Stack,
  StatusFlag,
  Text,
  Toggle,
  toast,
} from "~/ui";
import { setAgentToolEnabled, useAgentTools } from "../data";
import type { AgentTool } from "../model";

/** The agent's tool catalog, with a switch per tool (`AE-3.3`).
 *
 *  Presentation only: the catalog, each tool's description, and whether it is on all
 *  come from the backend, which derives them from the toolset registry the agent
 *  actually runs against — this panel never names a tool of its own. Grouped by
 *  category because that's how the catalog is organized and how an operator thinks
 *  about it ("turn off web", "turn off the shell"). */
export function AgentToolsPanel(): JSX.Element {
  const tools = useAgentTools();

  /** Category → its tools, in the order the backend listed them. */
  const grouped = createMemo(() => {
    const groups = new Map<string, AgentTool[]>();
    for (const tool of settled(tools) ?? []) {
      const bucket = groups.get(tool.category);
      if (bucket) bucket.push(tool);
      else groups.set(tool.category, [tool]);
    }
    return [...groups.entries()];
  });

  const offCount = createMemo(
    () => (settled(tools) ?? []).filter((t) => !t.enabled).length,
  );

  /* The hint below is worth its line only where a badge actually appears, and which
     groups start dormant is the backend's call — so ask the catalog rather than
     naming the groups here. */
  const hasDormant = createMemo(() =>
    (settled(tools) ?? []).some((t) => t.dormant),
  );

  const toggle = async (tool: AgentTool) => {
    try {
      await setAgentToolEnabled(tool.name, !tool.enabled);
    } catch {
      toast.error(
        `Unable to ${tool.enabled ? "disable" : "enable"} ${tool.name}.`,
      );
    }
  };

  return (
    <Stack gap={6}>
      <SettingsPageHeader
        section="agent-tools"
        status={
          <Show when={offCount()}>
            <StatusFlag>{`${offCount()} OFF`}</StatusFlag>
          </Show>
        }
      />
      <Show when={settled(tools)} fallback={<LoadingText />}>
        <Show
          when={(settled(tools) ?? []).length}
          fallback={<EmptyState icon="cpu" message="No tools registered" />}
        >
          <Stack gap={4}>
            <Text variant="caption" tone="dim">
              A tool switched off here is never offered to the agent and can
              never be invoked by it — on a live chat, on an unattended
              scheduled task, and on the resume of a run waiting for your
              approval alike.
            </Text>
            <Show when={hasDormant()}>
              <Text variant="caption" tone="dim">
                A tool marked DORMANT is on and available — it simply sits
                outside the agent's opening tool list, and the agent loads its
                group when a task calls for it.
              </Text>
            </Show>
            <For each={grouped()}>
              {([category, entries]) => (
                <Stack gap={3} class="pt-3">
                  <Text variant="plate" tone="dim">
                    {category}
                  </Text>
                  <For each={entries}>
                    {(tool) => (
                      <Row align="center" justify="between" gap={4}>
                        <Stack gap={1}>
                          <Row align="center" gap={2}>
                            <Text variant="label" tone="default">
                              {tool.name}
                            </Text>
                            {/* Idle rather than warn: deferred loading is the normal
                                resting state of these groups, not a fault to flag. */}
                            <Show when={tool.dormant}>
                              <StatusFlag>DORMANT</StatusFlag>
                            </Show>
                          </Row>
                          <CodeSpanText tone="dim">
                            {tool.description}
                          </CodeSpanText>
                        </Stack>
                        <Toggle
                          checked={tool.enabled}
                          onChange={() => void toggle(tool)}
                        />
                      </Row>
                    )}
                  </For>
                </Stack>
              )}
            </For>
          </Stack>
        </Show>
      </Show>
    </Stack>
  );
}
