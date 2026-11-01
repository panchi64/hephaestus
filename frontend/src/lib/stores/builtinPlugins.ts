/** Which built-in plugins the operator has switched on. Loaded and changed through
 *  `~/features/plugins`; read by anything that draws a plugin's UI. */
import { createSignal } from "solid-js";

const [switches, setSwitches] = createSignal<Readonly<Record<string, boolean>>>(
  {},
);
let defaults: Readonly<Record<string, boolean>> = {};

/** Each plugin's state until the backend answers, so a page doesn't blink while the
 *  list loads; a plugin not named here reads as on. */
export function declarePluginDefaults(
  next: Readonly<Record<string, boolean>>,
): void {
  defaults = { ...defaults, ...next };
}

export function pluginEnabled(name: string): boolean {
  return switches()[name] ?? defaults[name] ?? true;
}

export function setPluginSwitches(
  next: Readonly<Record<string, boolean>>,
): void {
  setSwitches(next);
}

export function setPluginSwitch(name: string, enabled: boolean): void {
  setSwitches((current) => ({ ...current, [name]: enabled }));
}
