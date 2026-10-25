/** Every built-in plugin's UI, merged into the shell's tables.
 *
 *  A built-in plugin is compiled in, so its UI is listed here once; everything the
 *  shell draws from it (nav rows, transcript rows, envelope labels) is derived from
 *  this list. Whether a plugin is switched on is runtime state, read separately
 *  (`~/lib/stores/builtinPlugins`). */

import type { NavItem } from "~/app/nav/types";
import type { EnvelopeLabel } from "~/features/chat/envelopeLabels";
import type { ToolEntry } from "~/features/chat/toolPresentation";
import { declarePluginDefaults } from "~/lib/stores/builtinPlugins";
import type { IconName } from "~/ui";
import { discordPlugin } from "./discord/plugin";
import { mailPlugin } from "./mail/plugin";
import { routinesPlugin } from "./routines/plugin";
import type { BuiltinPluginUi, PluginSettingsSection } from "./types";

export const BUILTIN_PLUGINS: readonly BuiltinPluginUi[] = [
  mailPlugin,
  routinesPlugin,
  discordPlugin,
];

declarePluginDefaults(
  Object.fromEntries(
    BUILTIN_PLUGINS.flatMap((p) =>
      p.defaultEnabled === undefined ? [] : [[p.name, p.defaultEnabled]],
    ),
  ),
);

/** A settings section and the plugin it belongs to. */
export interface PluginSettingsEntry {
  plugin: string;
  section: PluginSettingsSection;
}

export const PLUGIN_SETTINGS: readonly PluginSettingsEntry[] =
  BUILTIN_PLUGINS.flatMap((plugin) =>
    plugin.settings ? [{ plugin: plugin.name, section: plugin.settings }] : [],
  );

/** A nav row and the plugin it belongs to. */
export interface PluginNavItem {
  plugin: string;
  item: NavItem;
}

export const PLUGIN_NAV: readonly PluginNavItem[] = BUILTIN_PLUGINS.flatMap(
  (plugin) => (plugin.nav ?? []).map((item) => ({ plugin: plugin.name, item })),
);

export const PLUGIN_TOOLS: Readonly<Record<string, ToolEntry>> = Object.assign(
  {},
  ...BUILTIN_PLUGINS.map((plugin) => plugin.tools ?? {}),
);

export const PLUGIN_CATEGORY_ICONS: Readonly<Record<string, IconName>> =
  Object.fromEntries(
    BUILTIN_PLUGINS.flatMap((p) =>
      p.toolCategory ? [[p.toolCategory.name, p.toolCategory.icon]] : [],
    ),
  );

export const PLUGIN_CATEGORY_PREPARING: Readonly<Record<string, string>> =
  Object.fromEntries(
    BUILTIN_PLUGINS.flatMap((p) =>
      p.toolCategory ? [[p.toolCategory.name, p.toolCategory.preparing]] : [],
    ),
  );

export const PLUGIN_ENVELOPES: Readonly<Record<string, EnvelopeLabel>> =
  Object.assign({}, ...BUILTIN_PLUGINS.map((plugin) => plugin.envelopes ?? {}));

/** The plugin that owns a page, if one does. */
export function pluginOfHref(href: string): string | undefined {
  return PLUGIN_NAV.find((entry) => entry.item.href === href)?.plugin;
}
