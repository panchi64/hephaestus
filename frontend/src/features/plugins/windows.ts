/** Plugin windows: which ones the live plugins offer, and a session to show one. */
import { createResource, type Resource } from "solid-js";
import type { WindowSpec } from "~/lib/stores/pluginWindows";
import { api } from "~/lib/api";
import { apiUrl } from "~/lib/config";

export interface WindowOut {
  plugin_id: string;
  plugin: string;
  window: string;
  title: string;
  width: number;
  height: number;
  min_width: number;
  min_height: number;
  resizable: boolean;
}

interface WindowSessionOut {
  src: string;
  bridge_token: string;
  permissions: string[];
}

export interface WindowSession {
  /** Absolute URL of the window's entry page. */
  src: string;
  /** What the window's bridge calls are made with, standing for this window alone. */
  bridgeToken: string;
  /** What the plugin declared its windows may reach through the bridge. */
  permissions: string[];
}

export function toWindowSpec(dto: WindowOut): WindowSpec {
  return {
    pluginId: dto.plugin_id,
    plugin: dto.plugin,
    window: dto.window,
    title: dto.title,
    width: dto.width,
    height: dto.height,
    minWidth: dto.min_width,
    minHeight: dto.min_height,
    resizable: dto.resizable,
  };
}

export async function fetchPluginWindows(): Promise<WindowSpec[]> {
  return (await api.get<WindowOut[]>("/plugins/windows")).map(toWindowSpec);
}

/** The windows on offer, and a refresh that keeps the current list until the next
 *  one arrives. */
export function usePluginWindows(): [Resource<WindowSpec[]>, () => void] {
  const [data, { refetch }] = createResource(fetchPluginWindows);
  return [data, () => void refetch()];
}

export async function openWindowSession(
  pluginId: string,
  window: string,
): Promise<WindowSession> {
  const dto = await api.post<WindowSessionOut>(
    `/plugins/${encodeURIComponent(pluginId)}/windows/${encodeURIComponent(window)}/sessions`,
  );
  return {
    src: apiUrl(dto.src),
    bridgeToken: dto.bridge_token,
    permissions: dto.permissions,
  };
}
