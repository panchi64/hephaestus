import { type JSX } from "solid-js";
import { LibrarySection } from "~/features/library";
import { PluginsDirectoryScreen } from "../screens/PluginsDirectoryScreen";
import { PluginDetailScreen } from "../screens/PluginDetailScreen";

/** Plugins as one settings section: the directory, and the review screen for
 *  whichever plugin is open — the same shape as Skills and Agents. */
export function PluginsSection(): JSX.Element {
  return (
    <LibrarySection
      directory={(nav) => <PluginsDirectoryScreen onOpen={nav.view} />}
      detail={(id, nav) => <PluginDetailScreen id={id} onBack={nav.back} />}
    />
  );
}
