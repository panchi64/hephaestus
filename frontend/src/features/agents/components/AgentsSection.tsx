import { type JSX } from "solid-js";
import { LibrarySection } from "~/features/library";
import { AgentsDirectoryScreen } from "../screens/AgentsDirectoryScreen";
import { AgentDetailScreen } from "../screens/AgentDetailScreen";

/** Agents as one settings section: the directory, and the read view (with
 *  in-place editing) for whichever agent is open — the same shape as Skills. */
export function AgentsSection(): JSX.Element {
  return (
    <LibrarySection
      directory={(nav) => (
        <AgentsDirectoryScreen onOpen={nav.view} onEdit={nav.edit} />
      )}
      detail={(id, nav) => (
        <AgentDetailScreen id={id} onBack={nav.back} editing={nav.editing} />
      )}
    />
  );
}
