import { windowActions } from "~/app/plugin-windows";
import { confirm, toast } from "~/ui";
import {
  deletePlugin,
  discardDraft,
  exportPlugin,
  pluginErrorMessage,
  publishPlugin,
  unpublishPlugin,
} from "./data";
import {
  accessSummary,
  hasPendingDraft,
  publishTarget,
  type Plugin,
} from "./model";
import { fetchPluginWindows } from "./windows";

/** Confirms against the hosts and secrets the plugin will use, then publishes
 *  the draft (or re-enables a disabled plugin). Resolves true when it did. */
export async function publishWithConfirm(plugin: Plugin): Promise<boolean> {
  const target = publishTarget(plugin);
  if (!target) return false;
  const ok = await confirm({
    title: `Publish "${plugin.name}"?`,
    detail: `Its ${target.tools.length === 1 ? "tool becomes" : "tools become"} callable in the conversation's VM. ${accessSummary(target).join(" ")}`,
    confirmLabel: "Publish",
  });
  if (!ok) return false;
  try {
    await publishPlugin(plugin.id);
    toast.success(`"${plugin.name}" published`);
    return true;
  } catch (err) {
    toast.error(pluginErrorMessage(err, `Could not publish "${plugin.name}".`));
    return false;
  }
}

export async function unpublishWithToast(plugin: Plugin): Promise<boolean> {
  try {
    await unpublishPlugin(plugin.id);
    toast.success(`"${plugin.name}" disabled — its tools are off.`);
    return true;
  } catch (err) {
    toast.error(pluginErrorMessage(err, `Could not disable "${plugin.name}".`));
    return false;
  }
}

/** Resolves true when the plugin no longer exists afterwards. */
export async function discardWithConfirm(plugin: Plugin): Promise<boolean> {
  if (!hasPendingDraft(plugin)) return false;
  const ok = await confirm({
    title: `Discard the draft of "${plugin.name}"?`,
    detail: plugin.active
      ? "The draft is deleted. The published version keeps running."
      : "It has never been published, so the plugin is deleted with its draft.",
    confirmLabel: "Discard",
    tone: "alert",
  });
  if (!ok) return false;
  try {
    const left = await discardDraft(plugin.id);
    toast.success(left ? "Draft discarded" : `Deleted "${plugin.name}"`);
    return left === null;
  } catch (err) {
    toast.error(pluginErrorMessage(err, "Could not discard the draft."));
    return false;
  }
}

/** Resolves true when the plugin was deleted. */
export async function deleteWithConfirm(plugin: Plugin): Promise<boolean> {
  const ok = await confirm({
    title: `Delete "${plugin.name}"?`,
    detail:
      "The plugin, its versions and its stored secrets are permanently removed. Export it first if you want a copy.",
    confirmLabel: "Delete",
    tone: "alert",
  });
  if (!ok) return false;
  try {
    await deletePlugin(plugin.id);
    toast.success(`Deleted "${plugin.name}"`);
    return true;
  } catch (err) {
    toast.error(pluginErrorMessage(err, `Could not delete "${plugin.name}".`));
    return false;
  }
}

/** Opens every window a published plugin offers. */
export async function openWindowsWithToast(plugin: Plugin): Promise<void> {
  try {
    const viewport = { width: window.innerWidth, height: window.innerHeight };
    for (const spec of await fetchPluginWindows())
      if (spec.pluginId === plugin.id) windowActions.open(spec, viewport);
  } catch (err) {
    toast.error(pluginErrorMessage(err, `Could not open "${plugin.name}".`));
  }
}

export async function exportWithToast(plugin: Plugin): Promise<void> {
  try {
    await exportPlugin(plugin.id, plugin.name);
  } catch (err) {
    toast.error(pluginErrorMessage(err, `Could not export "${plugin.name}".`));
  }
}
