import {
  createEffect,
  createMemo,
  For,
  on,
  onCleanup,
  onMount,
  Show,
  type JSX,
} from "solid-js";
import { Portal } from "solid-js/web";
import { readLS, writeLS } from "~/lib/storage";
import { PluginWindow } from "./PluginWindow";
import {
  pluginWindows,
  windowActions,
  type PluginWindowState,
} from "~/lib/stores/pluginWindows";

const STORAGE_KEY = "heph.pluginWindows";

function viewport() {
  return { width: window.innerWidth, height: window.innerHeight };
}

function isWindowState(value: unknown): value is PluginWindowState {
  if (typeof value !== "object" || value === null) return false;
  const w = value as Record<string, unknown>;
  return (
    ["key", "pluginId", "plugin", "window", "title"].every(
      (k) => typeof w[k] === "string",
    ) &&
    ["x", "y", "width", "height", "minWidth", "minHeight"].every(
      (k) => typeof w[k] === "number" && Number.isFinite(w[k]),
    ) &&
    typeof w.resizable === "boolean"
  );
}

function loadSaved(): PluginWindowState[] {
  try {
    const parsed: unknown = JSON.parse(readLS(STORAGE_KEY) ?? "[]");
    return Array.isArray(parsed) ? parsed.filter(isWindowState) : [];
  } catch {
    return [];
  }
}

/** Every open plugin window, floating over the app on every route. Above the content
 *  and popovers' click-out backdrop, below modals and toasts, and clear of the pointer
 *  everywhere a window isn't. Which windows are open, and where, survives a reload. */
export function PluginWindowLayer(): JSX.Element {
  const persist = (): void =>
    writeLS(STORAGE_KEY, JSON.stringify(pluginWindows()));

  // Opening, closing and raising are saved as they happen; geometry once a drag settles.
  createEffect(
    on(
      () =>
        pluginWindows()
          .map((w) => w.key)
          .join(),
      persist,
      { defer: true },
    ),
  );

  onMount(() => {
    windowActions.restore(loadSaved(), viewport());
    const onResize = (): void => windowActions.fit(viewport());
    window.addEventListener("resize", onResize);
    onCleanup(() => window.removeEventListener("resize", onResize));
  });

  // Rendered by key in the order they opened, never re-sorted: moving an iframe in the
  // DOM reloads it, so raising a window changes its z-index, not its place.
  const mounted = createMemo<string[]>((previous) => {
    const open = pluginWindows().map((w) => w.key);
    const kept = previous.filter((key) => open.includes(key));
    return [...kept, ...open.filter((key) => !kept.includes(key))];
  }, []);
  const stacking = (key: string): number =>
    pluginWindows().findIndex((w) => w.key === key);

  return (
    <Portal>
      <div class="pointer-events-none fixed inset-0 z-[45]">
        <For each={mounted()}>
          {(key) => (
            <Show when={pluginWindows().find((w) => w.key === key)}>
              {(win) => (
                <PluginWindow
                  win={win()}
                  layer={stacking(key) + 1}
                  top={stacking(key) === pluginWindows().length - 1}
                  onSettled={persist}
                />
              )}
            </Show>
          )}
        </For>
      </div>
    </Portal>
  );
}
