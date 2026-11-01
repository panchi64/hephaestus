import { createEffect, on, Show, type JSX } from "solid-js";
import { usePluginWindows } from "~/features/plugins/windows";
import { settled } from "~/lib/resource";
import { Icon, Menu } from "~/ui";
import { pluginWindows, windowActions } from "~/lib/stores/pluginWindows";

/** The header's launcher for every window the live plugins offer. The list is read
 *  again whenever the menu is opened or a window opens, so a plugin written a moment
 *  ago is in it. */
export function PluginLauncher(): JSX.Element {
  const [resource, refresh] = usePluginWindows();
  const windows = () => settled(resource) ?? [];
  createEffect(on(() => pluginWindows().length, refresh, { defer: true }));

  return (
    <Show when={windows().length > 0}>
      <Menu
        trigger={
          <span
            aria-label="Plugin windows"
            class="flex items-center p-1 text-dim hover:text-text"
            onPointerDown={refresh}
          >
            <Icon name="grid" size={14} />
          </span>
        }
        items={windows().map((spec) => ({
          label: spec.title,
          icon: "plug",
          onSelect: () =>
            windowActions.open(spec, {
              width: window.innerWidth,
              height: window.innerHeight,
            }),
        }))}
      />
    </Show>
  );
}
