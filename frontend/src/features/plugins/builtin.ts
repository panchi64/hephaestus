/** The built-in plugins and the operator's switch for each (`/plugins/builtin`). */
import { createResource, createSignal, type Resource } from "solid-js";
import { api } from "~/lib/api";
import {
  setPluginSwitch,
  setPluginSwitches,
} from "~/lib/stores/builtinPlugins";

/** One contribution as its type describes it, rendered generically. */
export type DescribedItem = Record<
  string,
  string | number | boolean | string[] | null
>;

export interface BuiltinPluginOut {
  name: string;
  title: string;
  description: string;
  enabled: boolean;
  contributions: Record<string, DescribedItem[]>;
}

const PATH = "/plugins/builtin";
const [tick, setTick] = createSignal(0);

async function fetchBuiltinPlugins(): Promise<BuiltinPluginOut[]> {
  const plugins = await api.get<BuiltinPluginOut[]>(PATH);
  setPluginSwitches(
    Object.fromEntries(plugins.map((p) => [p.name, p.enabled])),
  );
  return plugins;
}

export function useBuiltinPlugins(): Resource<BuiltinPluginOut[]> {
  const [data] = createResource(tick, fetchBuiltinPlugins);
  return data;
}

/** Read the switches once at boot, so a switched-off plugin's pages leave the rail. */
export async function loadBuiltinPluginSwitches(): Promise<void> {
  await fetchBuiltinPlugins();
}

export async function switchBuiltinPlugin(
  name: string,
  enabled: boolean,
): Promise<void> {
  await api.put<BuiltinPluginOut>(`${PATH}/${encodeURIComponent(name)}`, {
    enabled,
  });
  setPluginSwitch(name, enabled);
  setTick((n) => n + 1);
}
