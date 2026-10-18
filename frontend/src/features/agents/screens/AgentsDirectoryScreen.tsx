import { createSignal, Show, type JSX } from "solid-js";
import { settled } from "~/lib/resource";
import { StatusFlag, Text, confirm, toast } from "~/ui";
import { compareByName, LibraryDirectory } from "~/features/library";
import {
  agentErrorMessage,
  deleteAgent,
  exportAgent,
  importAgent,
  setAgentPublished,
  useAgentClasses,
  useAgents,
} from "../data";
import {
  agentDisplayName,
  classDisplay,
  compareAgentsBySource,
  agentSourceTag,
  isAwaitingPublish,
  type Agent,
  type AgentClass,
} from "../model";
import { AgentStateFlags } from "../components/AgentStateFlags";
import { NewAgentDialog } from "../components/NewAgentDialog";

export function AgentsDirectoryScreen(props: {
  onOpen: (id: string) => void;
  /** Open an agent straight into editing. */
  onEdit: (id: string) => void;
}): JSX.Element {
  const agentsResource = useAgents();
  const agents = (): Agent[] => settled(agentsResource) ?? [];
  const classesResource = useAgentClasses();
  const classes = (): AgentClass[] => settled(classesResource) ?? [];
  /** Empty for Talos, whose class is its name. */
  const classLabel = (agent: Agent): string => {
    const display = classDisplay(classes(), agent.agentClass);
    return display === agentDisplayName(agent.name) ? "" : display;
  };
  const loadError = (): string | null =>
    agentsResource.error
      ? agentErrorMessage(agentsResource.error, "Could not load agents.")
      : null;
  const [newOpen, setNewOpen] = createSignal(false);
  const [importing, setImporting] = createSignal(false);

  async function handleImport(file: File | undefined): Promise<void> {
    if (!file || importing()) return;
    setImporting(true);
    try {
      const agent = await importAgent(file);
      toast.success(`Imported "${agent.name}" as a draft`, {
        action: { label: "Open", onClick: () => props.onOpen(agent.id) },
      });
    } catch (err) {
      toast.error(agentErrorMessage(err, "Could not import the agent file."));
    } finally {
      setImporting(false);
    }
  }

  async function handleDelete(agent: Agent): Promise<void> {
    const ok = await confirm({
      title: `Delete "${agent.name}"?`,
      detail:
        "The agent is permanently removed. Threads already running as it keep their record, but can no longer be sent to it. Export it first if you want a copy.",
      confirmLabel: "Delete",
      tone: "alert",
    });
    if (!ok) return;
    try {
      await deleteAgent(agent.id);
      toast.success(`Deleted "${agent.name}"`);
    } catch (err) {
      toast.error(agentErrorMessage(err, `Could not delete "${agent.name}"`));
    }
  }

  async function handlePublishToggle(agent: Agent): Promise<void> {
    const next = !agent.published;
    try {
      await setAgentPublished(agent.id, next);
    } catch (err) {
      toast.error(
        agentErrorMessage(
          err,
          `Could not ${next ? "publish" : "unpublish"} "${agent.name}"`,
        ),
      );
      return;
    }
    toast.success(
      next
        ? `"${agent.name}" published — threads can run as it now.`
        : `"${agent.name}" unpublished — back to draft.`,
      {
        action: {
          label: "Undo",
          onClick: () => {
            setAgentPublished(agent.id, !next).catch(() =>
              toast.error("Could not undo"),
            );
          },
        },
      },
    );
  }

  async function handleExport(agent: Agent): Promise<void> {
    try {
      await exportAgent(agent.id, agent.name);
    } catch (err) {
      toast.error(agentErrorMessage(err, `Could not export "${agent.name}"`));
    }
  }

  const awaiting = () => agents().filter(isAwaitingPublish).length;

  return (
    <LibraryDirectory
      title="Agents"
      subtitle="Named agents a thread can run as — their own instructions, tools and limits."
      assetId="HEPH-AGT-01.0"
      noun="agent"
      nouns="agents"
      icon="layers"
      items={agents()}
      loadError={loadError()}
      sorts={{
        name: {
          label: "Name",
          compare: (a, b) =>
            compareByName(
              { name: agentDisplayName(a.name) },
              { name: agentDisplayName(b.name) },
            ),
        },
        source: { label: "Source", compare: compareAgentsBySource },
      }}
      initialSort="name"
      initialDir="asc"
      bandExtra={[
        {
          label: "Awaiting you",
          value: String(awaiting()),
          tone: awaiting() > 0 ? "bright" : "dim",
        },
      ]}
      draftHint="A draft is not offered to threads. Publishing is what lets a thread run as the agent — imported files and agent-drafted agents always land as drafts so you can read them first."
      importHint="Accepts an agent file (.md) — a name and description in frontmatter, with the instructions as the body. It lands as a draft."
      importAccept=".md"
      importing={importing()}
      onImport={(file) => void handleImport(file)}
      onNew={() => setNewOpen(true)}
      onOpen={(agent) => props.onOpen(agent.id)}
      emptyCreateHint="Create an agent or import an agent file."
      rowLabel={(agent) => agentDisplayName(agent.name)}
      rowRight={(agent) => (
        <>
          <Show when={classLabel(agent)}>
            {(label) => (
              <Text variant="micro" tone="dim">
                {label()}
              </Text>
            )}
          </Show>
          <Show when={agentSourceTag(agent.source)}>
            {(tag) => <StatusFlag status="idle">{tag()}</StatusFlag>}
          </Show>
          <AgentStateFlags agent={agent} />
        </>
      )}
      menuItems={(agent) =>
        agent.editable
          ? [
              {
                label: "Edit",
                icon: "edit",
                onSelect: () => props.onEdit(agent.id),
              },
              {
                label: "Export",
                icon: "download",
                onSelect: () => void handleExport(agent),
              },
              {
                label: agent.published ? "Unpublish" : "Publish",
                icon: "check",
                onSelect: () => void handlePublishToggle(agent),
              },
              {
                label: "Delete",
                icon: "trash",
                danger: true,
                onSelect: () => void handleDelete(agent),
              },
            ]
          : [
              {
                label: "Open",
                icon: "eye",
                onSelect: () => props.onOpen(agent.id),
              },
              {
                label: "Export",
                icon: "download",
                onSelect: () => void handleExport(agent),
              },
            ]
      }
    >
      <NewAgentDialog
        open={newOpen()}
        onClose={() => setNewOpen(false)}
        onCreated={(id) => {
          setNewOpen(false);
          props.onEdit(id);
        }}
      />
    </LibraryDirectory>
  );
}
