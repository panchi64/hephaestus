import { createSignal, type JSX } from "solid-js";
import { settled } from "~/lib/resource";
import { Text, toast } from "~/ui";
import {
  compareByName,
  compareByUpdated,
  LibraryDirectory,
} from "~/features/library";
import { importPlugin, pluginErrorMessage, usePlugins } from "../data";
import { contributionParts, hasPendingDraft, type Plugin } from "../model";
import { deleteWithConfirm, exportWithToast } from "../actions";
import { PluginStateFlags } from "../components/PluginStateFlags";

export function PluginsDirectoryScreen(props: {
  onOpen: (id: string) => void;
}): JSX.Element {
  const resource = usePlugins();
  const plugins = (): Plugin[] => settled(resource) ?? [];
  const loadError = (): string | null =>
    resource.error
      ? pluginErrorMessage(resource.error, "Could not load plugins.")
      : null;
  const [importing, setImporting] = createSignal(false);

  async function handleImport(file: File | undefined): Promise<void> {
    if (!file || importing()) return;
    setImporting(true);
    try {
      const plugin = await importPlugin(file);
      toast.success(`Imported "${plugin.name}" as a draft`, {
        action: { label: "Open", onClick: () => props.onOpen(plugin.id) },
      });
    } catch (err) {
      toast.error(pluginErrorMessage(err, "Could not import the plugin."));
    } finally {
      setImporting(false);
    }
  }

  const awaiting = () => plugins().filter(hasPendingDraft).length;

  return (
    <LibraryDirectory
      title="Plugins"
      subtitle="Tools, agents and skills that agents write. Python tools run in the conversation's VM once you review and publish them."
      assetId="HEPH-PLG-01.0"
      noun="plugin"
      nouns="plugins"
      icon="plug"
      items={plugins()}
      loadError={loadError()}
      sorts={{
        name: { label: "Name", compare: compareByName },
        updated: { label: "Updated", compare: compareByUpdated },
      }}
      initialSort="name"
      initialDir="asc"
      bandExtra={[
        {
          label: "Awaiting review",
          value: String(awaiting()),
          tone: awaiting() > 0 ? "bright" : "dim",
        },
      ]}
      draftHint="Nothing in a draft is used. Publishing is what lets agents use it — review the hosts, secrets, tools, agents and skills it adds first."
      importHint="Accepts a plugin zip. It lands as a draft so you can read it before publishing."
      importAccept=".zip"
      importing={importing()}
      onImport={(file) => void handleImport(file)}
      onOpen={(plugin) => props.onOpen(plugin.id)}
      emptyCreateHint="Plugins appear here when an agent drafts one, or when you import a zip."
      rowRight={(plugin) => (
        <>
          <Text variant="micro" tone="dim">
            {contributionParts(plugin).join(" · ").toUpperCase()}
          </Text>
          <PluginStateFlags plugin={plugin} />
        </>
      )}
      menuItems={(plugin) => [
        {
          label: "Open",
          icon: "eye",
          onSelect: () => props.onOpen(plugin.id),
        },
        {
          label: "Export",
          icon: "download",
          onSelect: () => void exportWithToast(plugin),
        },
        {
          label: "Delete",
          icon: "trash",
          danger: true,
          onSelect: () => void deleteWithConfirm(plugin),
        },
      ]}
    />
  );
}
