/** Which built-in plugins the operator has switched on. Loaded and changed through
 *  `~/features/plugins`; read by anything that draws a plugin's UI. */
import { createSignal } from "solid-js";

const [switches, setSwitches] = createSignal<Readonly<Record<string, boolean>>>(
  {},
);

/** On until the backend says otherwise, so a plugin's page doesn't blink "off" while
 *  the list loads. */
export function pluginEnabled(name: string): boolean {
  return switches()[name] ?? true;
}

export function setPluginSwitches(
  next: Readonly<Record<string, boolean>>,
): void {
  setSwitches(next);
}

export function setPluginSwitch(name: string, enabled: boolean): void {
  setSwitches((current) => ({ ...current, [name]: enabled }));
}
