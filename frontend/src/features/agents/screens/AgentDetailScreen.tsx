import { createSignal, Show, Suspense, type JSX } from "solid-js";
import {
  RevisionHistory,
  refreshRevisions,
  revisionsBase,
} from "~/features/revisions";
import { settled } from "~/lib/resource";
import { Button, EmptyState, LoadingText, Text, confirm, toast } from "~/ui";
import {
  LibraryDetail,
  type LibraryFileSource,
  type LibraryProperty,
} from "~/features/library";
import {
  agentErrorMessage,
  deleteAgent,
  exportAgent,
  getAgentDocument,
  putAgentDocument,
  refreshAgentDetail,
  setAgentPublished,
  updateAgent,
  useAgentClasses,
  useAgentDetail,
} from "../data";
import {
  agentDisplayName,
  agentSourceTag,
  classSpec,
  isAwaitingPublish,
  type Agent,
} from "../model";
import { AgentStateFlags } from "../components/AgentStateFlags";
import { ModelPicker, type ModelChoice } from "../components/ModelPicker";

function list(items: readonly string[]): string {
  return items.length ? items.join(", ") : "—";
}

function metaOf(a: Agent): string {
  const parts: string[] = [a.source === "builtin" ? "built-in" : a.source];
  if (!a.editable) parts.push("read-only");
  if (isAwaitingPublish(a))
    parts.push("drafted by an agent — review before publishing");
  return parts.join(" · ");
}

/** An agent's read view. The agent's file is edited in place; the model, which
 *  the file does not carry, is picked on the Overview while editing. */
export function AgentDetailScreen(props: {
  id: string;
  onBack: () => void;
  editing?: boolean;
}): JSX.Element {
  const resource = useAgentDetail(() => props.id);
  const agent = () => settled(resource) ?? undefined;
  const loadError = (): string | null =>
    resource.error
      ? agentErrorMessage(resource.error, "Could not load this agent.")
      : null;
  const [modelDraft, setModelDraft] = createSignal<ModelChoice | null>(null);

  const choiceOf = (a: Agent): ModelChoice =>
    modelDraft() ?? { endpointId: a.endpointId, model: a.model };
  const modelDirty = (a: Agent): boolean => {
    const d = modelDraft();
    return d !== null && (d.endpointId !== a.endpointId || d.model !== a.model);
  };

  const classes = useAgentClasses();
  /** Read-only even while editing: the agent's file carries `class:`, and one value
   *  edited in two places is how they drift. */
  const classOf = (a: Agent): string => {
    const spec = classSpec(settled(classes) ?? [], a.agentClass);
    return spec ? `${spec.display} — ${spec.role}` : a.agentClass;
  };

  function propertiesOf(a: Agent, editing: boolean): LibraryProperty[] {
    return [
      { label: "Class", value: classOf(a) },
      { label: "Source", value: agentSourceTag(a.source) ?? "USER" },
      {
        label: "Model",
        value:
          editing && a.editable ? (
            <ModelPicker value={choiceOf(a)} onChange={setModelDraft} />
          ) : (
            (a.model ?? "Thread's model")
          ),
      },
      {
        label: "Tools",
        value: a.tools === null ? "Every tool" : list(a.tools),
      },
      { label: "Denied tools", value: list(a.denyTools) },
      { label: "Permission ceiling", value: a.permissionCeiling ?? "None" },
      { label: "Workspace", value: a.workspace },
      {
        label: "Wall-clock timeout",
        value:
          a.wallClockTimeoutS === null
            ? "No timeout"
            : `${a.wallClockTimeoutS} s`,
      },
      {
        label: "Can delegate",
        value: a.canDelegate
          ? `Yes · max depth ${a.maxDepth ?? "default"}`
          : "No",
      },
    ];
  }

  async function togglePublished(a: Agent): Promise<void> {
    const next = !a.published;
    try {
      await setAgentPublished(a.id, next);
      refreshRevisions();
      toast.success(
        next
          ? "Published — threads can run as this agent now."
          : "Unpublished — back to draft.",
      );
    } catch (err) {
      toast.error(
        agentErrorMessage(
          err,
          `Could not ${next ? "publish" : "unpublish"} the agent.`,
        ),
      );
    }
  }

  async function remove(a: Agent): Promise<void> {
    const ok = await confirm({
      title: `Delete "${a.name}"?`,
      detail:
        "The agent is permanently removed. Export it first if you want a copy.",
      confirmLabel: "Delete",
      tone: "alert",
    });
    if (!ok) return;
    try {
      await deleteAgent(a.id);
      toast.success(`Deleted "${a.name}"`);
      props.onBack();
    } catch (err) {
      toast.error(agentErrorMessage(err, `Could not delete "${a.name}"`));
    }
  }

  async function exportIt(a: Agent): Promise<void> {
    try {
      await exportAgent(a.id, a.name);
    } catch (err) {
      toast.error(agentErrorMessage(err, "Could not export the agent."));
    }
  }

  const filesOf = (a: Agent): LibraryFileSource[] => [
    { path: `${a.name}.md`, load: () => getAgentDocument(a.id) },
  ];

  return (
    <Suspense fallback={<LoadingText label="Loading agent" />}>
      <Show
        when={agent()}
        fallback={
          <EmptyState
            icon="layers"
            message={loadError() ? "Agent unavailable" : "Agent not found"}
            hint={loadError() ?? "The requested agent does not exist."}
            action={
              <Button
                variant="default"
                leading="chevron-left"
                onClick={props.onBack}
              >
                Back to agents
              </Button>
            }
          />
        }
      >
        {(a) => (
          <LibraryDetail
            backLabel="Agents"
            onBack={props.onBack}
            icon="layers"
            name={agentDisplayName(a().name)}
            meta={metaOf(a())}
            flags={
              <>
                <AgentStateFlags agent={a()} awaiting />
                <Show when={!a().editable}>
                  <Text variant="micro" tone="dim">
                    READ-ONLY
                  </Text>
                </Show>
              </>
            }
            published={a().published}
            onTogglePublished={
              a().editable ? () => void togglePublished(a()) : undefined
            }
            onExport={() => void exportIt(a())}
            canEdit={a().editable}
            startEditing={props.editing}
            menuItems={
              a().editable
                ? [
                    {
                      label: a().published ? "Unpublish" : "Publish",
                      icon: "check",
                      onSelect: () => void togglePublished(a()),
                    },
                    {
                      label: "Delete",
                      icon: "trash",
                      danger: true,
                      onSelect: () => void remove(a()),
                    },
                  ]
                : []
            }
            description={a().description}
            properties={(editing) => propertiesOf(a(), editing)}
            files={filesOf(a())}
            saveFile={async (_path, text) => {
              await putAgentDocument(a().id, text);
              refreshRevisions();
            }}
            history={
              a().editable ? (
                <RevisionHistory
                  base={revisionsBase("agents", a().id)}
                  onRestored={refreshAgentDetail}
                />
              ) : undefined
            }
            extraDirty={modelDirty(a())}
            saveExtra={async () => {
              const d = modelDraft();
              if (d) {
                await updateAgent(a().id, {
                  endpoint_id: d.endpointId,
                  model: d.model,
                });
              }
              setModelDraft(null);
            }}
            resetExtra={() => setModelDraft(null)}
          />
        )}
      </Show>
    </Suspense>
  );
}
