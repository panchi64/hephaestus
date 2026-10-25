import { type JSX } from "solid-js";
import { LibrarySection } from "~/features/library";
import { PluginsDirectoryScreen } from "../screens/PluginsDirectoryScreen";
import { PluginDetailScreen } from "../screens/PluginDetailScreen";
import { BuiltinPluginsPanel } from "./BuiltinPluginsPanel";

/** Plugins as one settings section: the built-in plugins' switches, the directory of
 *  plugins agents write, and the review screen for whichever is open. */
export function PluginsSection(): JSX.Element {
  return (
    <LibrarySection
      directory={(nav) => (
        <div class="flex flex-col gap-6">
          <PluginsDirectoryScreen onOpen={nav.view} />
          <BuiltinPluginsPanel />
        </div>
      )}
      detail={(id, nav) => <PluginDetailScreen id={id} onBack={nav.back} />}
    />
  );
}
