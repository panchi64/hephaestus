import { createSignal, For, Show, Suspense, type JSX } from "solid-js";
import { settled } from "~/lib/resource";
import {
  Button,
  EmptyState,
  ListGroupHeader,
  ListRow,
  LoadingText,
  Menu,
  PageHeader,
  Panel,
  Stack,
  Text,
  InstrumentBand,
  confirm,
  toast,
} from "~/ui";
import {
  agentErrorMessage,
  deleteAgent,
  setAgentPublished,
  useAgents,
} from "../data";
import { groupAgentsBySource, isAwaitingPublish, type Agent } from "../model";
import { AgentStateFlags } from "../components/AgentStateFlags";
import { NewAgentDialog } from "../components/NewAgentDialog";

export function AgentsDirectoryScreen(props: {
  onOpen: (id: string) => void;
}): JSX.Element {
  const agentsResource = useAgents();
  const agents = (): Agent[] => settled(agentsResource) ?? [];
  const loadError = (): string | null =>
    agentsResource.error
      ? agentErrorMessage(agentsResource.error, "Could not load agents.")
      : null;
  const groups = () => groupAgentsBySource(agents());
  const [newOpen, setNewOpen] = createSignal(false);

  async function handleDelete(agent: Agent): Promise<void> {
    const ok = await confirm({
      title: `Delete "${agent.name}"?`,
      detail:
        "The agent is permanently removed. Threads already running as it keep their record, but can no longer be sent to it.",
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
      toast.success(
        next
          ? `"${agent.name}" published — threads can run as it now.`
          : `"${agent.name}" unpublished — back to draft.`,
      );
    } catch (err) {
      toast.error(
        agentErrorMessage(
          err,
          `Could not ${next ? "publish" : "unpublish"} "${agent.name}"`,
        ),
      );
    }
  }

  const awaiting = () => agents().filter(isAwaitingPublish).length;
  const published = () => agents().filter((a) => a.published).length;

  return (
    <Stack gap={6}>
      <PageHeader
        variant="section"
        title="Agents"
        subtitle="Named agents a thread can run as — their own instructions, tools and limits."
        assetId="ODY-AGT-01.0"
        actions={
          <Button
            variant="primary"
            leading="plus"
            onClick={() => setNewOpen(true)}
          >
            New agent
          </Button>
        }
      />

      <InstrumentBand
        items={[
          { label: "Total", value: String(agents().length) },
          { label: "Published", value: String(published()), tone: "nominal" },
          {
            label: "Awaiting you",
            value: String(awaiting()),
            tone: awaiting() > 0 ? "bright" : "dim",
          },
        ]}
      />

      <Panel flush>
        <Suspense
          fallback={
            <div class="p-4">
              <LoadingText />
            </div>
          }
        >
          <Show
            when={groups().length}
            fallback={
              <EmptyState
                icon="layers"
                message={loadError() ? "Agents unavailable" : "No agents"}
                hint={loadError() ?? "Create an agent to get started."}
              />
            }
          >
            <For each={groups()}>
              {(group) => (
                <>
                  <ListGroupHeader
                    variant="plate"
                    label={group.label}
                    count={group.agents.length}
                  />
                  <For each={group.agents}>
                    {(agent) => (
                      <ListRow
                        label={agent.name}
                        description={agent.description}
                        leading="layers"
                        onClick={() => props.onOpen(agent.id)}
                        right={
                          <span class="flex shrink-0 items-center gap-3">
                            <AgentStateFlags agent={agent} />
                            <Show when={!agent.editable}>
                              <Text variant="micro" tone="dim">
                                READ-ONLY
                              </Text>
                            </Show>
                            <Show when={agent.editable}>
                              <span
                                onClick={(e) => {
                                  e.preventDefault();
                                  e.stopPropagation();
                                }}
                              >
                                <Menu
                                  trigger={
                                    <span class="px-1 text-dim hover:text-bright">
                                      <Text variant="micro">···</Text>
                                    </span>
                                  }
                                  items={[
                                    {
                                      label: "Edit",
                                      icon: "edit",
                                      onSelect: () => props.onOpen(agent.id),
                                    },
                                    {
                                      label: agent.published
                                        ? "Unpublish"
                                        : "Publish",
                                      icon: "check",
                                      onSelect: () =>
                                        void handlePublishToggle(agent),
                                    },
                                    {
                                      label: "Delete",
                                      icon: "trash",
                                      danger: true,
                                      onSelect: () => void handleDelete(agent),
                                    },
                                  ]}
                                />
                              </span>
                            </Show>
                          </span>
                        }
                      />
                    )}
                  </For>
                </>
              )}
            </For>
          </Show>
        </Suspense>
      </Panel>

      <NewAgentDialog
        open={newOpen()}
        onClose={() => setNewOpen(false)}
        onCreated={(id) => {
          setNewOpen(false);
          props.onOpen(id);
        }}
      />
    </Stack>
  );
}
