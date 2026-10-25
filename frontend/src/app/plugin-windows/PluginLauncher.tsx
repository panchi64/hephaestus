import { createSignal, Show, type JSX } from "solid-js";
import { usePluginWindows } from "~/features/plugins/windows";
import { settled } from "~/lib/resource";
import { Icon, Menu } from "~/ui";
import { windowActions } from "./windowStore";

/** The header's launcher for every window the live plugins offer. The list is read
 *  each time the menu is opened, so a plugin written a moment ago is in it. */
export function PluginLauncher(): JSX.Element {
  const [opened, setOpened] = createSignal(0);
  const resource = usePluginWindows(opened);
  const windows = () => settled(resource) ?? [];

  return (
    <Show when={windows().length > 0}>
      <div onPointerDown={() => setOpened((n) => n + 1)}>
        <Menu
          trigger={
            <span
              aria-label="Plugin windows"
              class="flex items-center p-1 text-dim hover:text-text"
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
      </div>
    </Show>
  );
}
