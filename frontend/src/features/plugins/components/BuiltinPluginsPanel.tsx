import { For, Show, type JSX } from "solid-js";
import { settled } from "~/lib/resource";
import { Panel, Text, Toggle, toast } from "~/ui";
import {
  switchBuiltinPlugin,
  useBuiltinPlugins,
  type BuiltinPluginOut,
} from "../builtin";

function contributionSummary(plugin: BuiltinPluginOut): string {
  return Object.entries(plugin.contributions)
    .map(([kind, items]) => `${items.length} ${kind}`)
    .join(" · ");
}

/** The plugins that ship with the platform, each with its switch. Switching one off
 *  takes its pages, tools and background work away until it is switched back on. */
export function BuiltinPluginsPanel(): JSX.Element {
  const resource = useBuiltinPlugins();
  const plugins = (): BuiltinPluginOut[] => settled(resource) ?? [];

  async function flip(
    plugin: BuiltinPluginOut,
    enabled: boolean,
  ): Promise<void> {
    try {
      await switchBuiltinPlugin(plugin.name, enabled);
      toast.success(
        `${plugin.title} ${enabled ? "switched on" : "switched off"}`,
      );
    } catch {
      toast.error(
        `Could not switch ${plugin.title} ${enabled ? "on" : "off"}.`,
      );
    }
  }

  return (
    <Show when={plugins().length > 0}>
      <Panel label="Built-in">
        <ul class="flex flex-col divide-y divide-line">
          <For each={plugins()}>
            {(plugin) => (
              <li class="flex items-start justify-between gap-4 py-3">
                <div class="flex min-w-0 flex-col gap-1">
                  <Text variant="label">{plugin.title}</Text>
                  <Text variant="micro" tone="dim">
                    {plugin.description}
                  </Text>
                  <Show when={contributionSummary(plugin)}>
                    <Text variant="micro" tone="dim">
                      {contributionSummary(plugin).toUpperCase()}
                    </Text>
                  </Show>
                </div>
                <Toggle
                  checked={plugin.enabled}
                  label={plugin.enabled ? "On" : "Off"}
                  onChange={(next) => void flip(plugin, next)}
                />
              </li>
            )}
          </For>
        </ul>
      </Panel>
    </Show>
  );
}
