import {
  createEffect,
  createSignal,
  on,
  Show,
  Suspense,
  type JSX,
} from "solid-js";
import { createStore } from "solid-js/store";
import { settled } from "~/lib/resource";
import {
  Button,
  EditorShell,
  EmptyState,
  LoadingText,
  Panel,
  Stack,
  Text,
  Textarea,
  confirm,
  toast,
} from "~/ui";
import {
  agentErrorField,
  agentErrorMessage,
  deleteAgent,
  setAgentPublished,
  updateAgent,
  useAgentDetail,
} from "../data";
import {
  buildAgentPatch,
  draftOf,
  emptyDraft,
  isAwaitingPublish,
  isDraftDirty,
  type Agent,
  type AgentDraft,
} from "../model";
import { AgentFields, type AgentFieldErrors } from "../components/AgentFields";
import { AgentStateFlags } from "../components/AgentStateFlags";

/** Full-page agent editor: instructions in the main column, the rest of the
 *  agent's definition in the aside. A non-editable agent (built-in, project
 *  or plugin) opens the same layout read-only. */
export function AgentEditorScreen(props: {
  id: string;
  onBack: () => void;
}): JSX.Element {
  const agentResource = useAgentDetail(() => props.id);
  const agent = () => settled(agentResource) ?? undefined;
  const loadError = (): string | null =>
    agentResource.error
      ? agentErrorMessage(agentResource.error, "Could not load this agent.")
      : null;

  const [draft, setDraft] = createStore<AgentDraft>(emptyDraft());
  const [errors, setErrors] = createSignal<AgentFieldErrors>({});
  const [seeded, setSeeded] = createSignal(false);
  const [showSaved, setShowSaved] = createSignal(false);
  const [busy, setBusy] = createSignal(false);

  createEffect(
    on(
      () => props.id,
      () => {
        setSeeded(false);
        setErrors({});
      },
      { defer: true },
    ),
  );
  createEffect(() => {
    const a = agent();
    if (seeded() || !a) return;
    setDraft(draftOf(a));
    setSeeded(true);
  });

  const isDirty = () => {
    const a = agent();
    return seeded() && a !== undefined && a.editable && isDraftDirty(draft, a);
  };

  function setField<K extends keyof AgentDraft>(
    key: K,
    value: AgentDraft[K],
  ): void {
    setDraft(key, value);
  }

  async function handleSave(): Promise<boolean> {
    const a = agent();
    if (!a || busy()) return false;
    if (!isDirty()) return true;
    setBusy(true);
    setErrors({});
    try {
      const saved = await updateAgent(a.id, buildAgentPatch(draft, a));
      setDraft(draftOf(saved));
      setShowSaved(true);
      toast.success("Agent saved");
      setTimeout(() => setShowSaved(false), 2000);
      return true;
    } catch (err) {
      const message = agentErrorMessage(err, "Could not save the agent.");
      const field = agentErrorField(err);
      if (field) setErrors({ [field]: message });
      else toast.error(message);
      return false;
    } finally {
      setBusy(false);
    }
  }

  async function handlePublishToggle(): Promise<void> {
    const a = agent();
    if (!a) return;
    const next = !a.published;
    try {
      if (isDirty() && !(await handleSave())) return;
      await setAgentPublished(a.id, next);
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

  async function handleDelete(a: Agent): Promise<void> {
    const ok = await confirm({
      title: `Delete "${a.name}"?`,
      detail: "The agent is permanently removed.",
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

  const asidePanel = (a: Agent) => () => (
    <>
      <Panel label="Definition">
        <AgentFields
          draft={draft}
          onChange={setField}
          errors={errors()}
          disabled={busy() || !a.editable}
        />
      </Panel>
      <Show when={a.editable}>
        <Panel label="Actions">
          <Stack gap={2}>
            <Button
              variant="default"
              size="sm"
              leading="check"
              block
              onClick={() => void handlePublishToggle()}
            >
              {a.published ? "Unpublish" : "Publish"}
            </Button>
            <Button
              variant="default"
              size="sm"
              leading="trash"
              block
              onClick={() => void handleDelete(a)}
            >
              Delete
            </Button>
          </Stack>
        </Panel>
      </Show>
    </>
  );

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
          <EditorShell
            onBack={props.onBack}
            backLabel="Back to agents"
            title={draft.name || a().name}
            dirty={isDirty()}
            asideLabel="Definition"
            meta={
              <Text variant="micro" tone="dim">
                {a().source.toUpperCase()}
                {a().editable ? "" : " · READ-ONLY"}
                {isAwaitingPublish(a())
                  ? " · DRAFTED BY AN AGENT — REVIEW BEFORE PUBLISHING"
                  : ""}
              </Text>
            }
            status={<AgentStateFlags agent={a()} />}
            actions={
              <Show when={a().editable}>
                <Button
                  variant={showSaved() ? "default" : "primary"}
                  leading={showSaved() ? "check" : "download"}
                  size="sm"
                  disabled={!isDirty() || busy()}
                  onClick={() => void handleSave()}
                >
                  {showSaved() ? "Saved" : "Save"}
                </Button>
              </Show>
            }
            aside={asidePanel(a())}
          >
            <Textarea
              value={draft.instructions}
              readOnly={!a().editable}
              onInput={(e) => setField("instructions", e.currentTarget.value)}
              rows={32}
              class="h-full w-full resize-none font-mono text-body"
            />
          </EditorShell>
        )}
      </Show>
    </Suspense>
  );
}
