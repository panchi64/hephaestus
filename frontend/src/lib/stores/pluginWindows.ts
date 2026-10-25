/** The plugin windows floating over the app: which are open, where, and in what
 *  stacking order (the list's order — last is on top). Transitions only; loading a
 *  window's session, persisting geometry and the frames themselves live in the layer. */
import { createSignal } from "solid-js";

/** What a plugin declares about one of its windows. */
export interface WindowSpec {
  pluginId: string;
  plugin: string;
  window: string;
  title: string;
  width: number;
  height: number;
  minWidth: number;
  minHeight: number;
  resizable: boolean;
}

export interface PluginWindowState extends WindowSpec {
  key: string;
  x: number;
  y: number;
}

export interface Viewport {
  width: number;
  height: number;
}

/** How much of a window must stay on screen, so a dragged-off one can be grabbed back. */
const GRAB_MARGIN = 48;
/** Where a new window lands relative to the last one opened, so they don't stack exactly. */
const CASCADE = 24;

export function windowKey(
  spec: Pick<WindowSpec, "pluginId" | "window">,
): string {
  return `${spec.pluginId}:${spec.window}`;
}

export function clampWindow(
  win: PluginWindowState,
  viewport: Viewport,
): PluginWindowState {
  const width = Math.max(win.minWidth, Math.min(win.width, viewport.width));
  const height = Math.max(win.minHeight, Math.min(win.height, viewport.height));
  const x = Math.min(
    Math.max(win.x, GRAB_MARGIN - width),
    viewport.width - GRAB_MARGIN,
  );
  const y = Math.min(Math.max(win.y, 0), viewport.height - GRAB_MARGIN);
  return { ...win, width, height, x, y };
}

function raise(
  list: readonly PluginWindowState[],
  key: string,
): PluginWindowState[] {
  const found = list.find((w) => w.key === key);
  return found ? [...list.filter((w) => w.key !== key), found] : [...list];
}

/** Opens a window, or brings an open one to the top. */
export function openWindow(
  list: readonly PluginWindowState[],
  spec: WindowSpec,
  viewport: Viewport,
): PluginWindowState[] {
  const key = windowKey(spec);
  if (list.some((w) => w.key === key)) return raise(list, key);
  const offset = list.length * CASCADE;
  const placed: PluginWindowState = {
    ...spec,
    key,
    x: viewport.width - spec.width - 32 - offset,
    y: 72 + offset,
  };
  return [...list, clampWindow(placed, viewport)];
}

export function closeWindow(
  list: readonly PluginWindowState[],
  key: string,
): PluginWindowState[] {
  return list.filter((w) => w.key !== key);
}

export function moveWindow(
  list: readonly PluginWindowState[],
  key: string,
  dx: number,
  dy: number,
  viewport: Viewport,
): PluginWindowState[] {
  return list.map((w) =>
    w.key === key
      ? clampWindow({ ...w, x: w.x + dx, y: w.y + dy }, viewport)
      : w,
  );
}

export function resizeWindow(
  list: readonly PluginWindowState[],
  key: string,
  dw: number,
  dh: number,
  viewport: Viewport,
): PluginWindowState[] {
  return list.map((w) =>
    w.key === key && w.resizable
      ? clampWindow(
          { ...w, width: w.width + dw, height: w.height + dh },
          viewport,
        )
      : w,
  );
}

const [windows, setWindows] = createSignal<readonly PluginWindowState[]>([]);

export const pluginWindows = windows;

export const windowActions = {
  open: (spec: WindowSpec, viewport: Viewport): void => {
    setWindows((list) => openWindow(list, spec, viewport));
  },
  close: (key: string): void => {
    setWindows((list) => closeWindow(list, key));
  },
  focus: (key: string): void => {
    setWindows((list) => (list.at(-1)?.key === key ? list : raise(list, key)));
  },
  move: (key: string, dx: number, dy: number, viewport: Viewport): void => {
    setWindows((list) => moveWindow(list, key, dx, dy, viewport));
  },
  resize: (key: string, dw: number, dh: number, viewport: Viewport): void => {
    setWindows((list) => resizeWindow(list, key, dw, dh, viewport));
  },
  /** Every window back on screen after the viewport shrank. */
  fit: (viewport: Viewport): void => {
    setWindows((list) => list.map((w) => clampWindow(w, viewport)));
  },
  restore: (saved: readonly PluginWindowState[], viewport: Viewport): void => {
    setWindows(saved.map((w) => clampWindow(w, viewport)));
  },
};
