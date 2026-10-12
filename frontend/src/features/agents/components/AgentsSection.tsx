import { createSignal, Show, type JSX } from "solid-js";
import { AgentsDirectoryScreen } from "../screens/AgentsDirectoryScreen";
import { AgentEditorScreen } from "../screens/AgentEditorScreen";

/** Agents as one settings section, two levels deep: the directory, and the
 *  editor (or read-only view) for whichever agent is open. The open id lives
 *  here, as it does for Skills, because the dialog has no route to carry it. */
export function AgentsSection(): JSX.Element {
  const [openId, setOpenId] = createSignal<string | null>(null);
  return (
    <Show
      when={openId()}
      keyed
      fallback={<AgentsDirectoryScreen onOpen={setOpenId} />}
    >
      {(id) => <AgentEditorScreen id={id} onBack={() => setOpenId(null)} />}
    </Show>
  );
}
