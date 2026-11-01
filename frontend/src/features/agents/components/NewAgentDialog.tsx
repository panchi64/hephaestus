import { createEffect, createSignal, Show, type JSX } from "solid-js";
import { Button, Modal, Select, Stack, Text } from "~/ui";
import { IdentityFields } from "~/features/library";
import { settled } from "~/lib/resource";
import {
  agentErrorField,
  agentErrorMessage,
  createAgent,
  useAgentClasses,
} from "../data";
import { AGENT_DESCRIPTION_MAX, AGENT_NAME_PATTERN } from "../model";
import { ModelPicker, type ModelChoice } from "./ModelPicker";

const NO_MODEL: ModelChoice = { endpointId: null, model: null };

type Errors = Partial<Record<"name" | "description" | "agent_class", string>>;

/** Create an agent from the fields that identify it (name, description, class) and the
 *  model it runs on. Everything else is set in the editor; a new agent starts as a draft. */
export function NewAgentDialog(props: {
  open: boolean;
  onClose: () => void;
  onCreated: (id: string) => void;
}): JSX.Element {
  const [name, setName] = createSignal("");
  const [description, setDescription] = createSignal("");
  // Unset means the backend's default class, which is also what the picker shows.
  const [agentClass, setAgentClass] = createSignal<string | undefined>();
  const [model, setModel] = createSignal<ModelChoice>(NO_MODEL);
  const [errors, setErrors] = createSignal<Errors>({});
  const [formError, setFormError] = createSignal("");
  const [busy, setBusy] = createSignal(false);
  const classes = useAgentClasses();
  const classOptions = () =>
    (settled(classes)?.classes ?? [])
      .filter((c) => c.assignable)
      .map((c) => ({
        value: c.key,
        label: c.display,
        description: c.role,
      }));
  const shownClass = () => agentClass() ?? settled(classes)?.defaultClass;

  createEffect(() => {
    if (!props.open) return;
    setName("");
    setDescription("");
    setAgentClass(undefined);
    setModel(NO_MODEL);
    setErrors({});
    setFormError("");
  });

  // No Create before the catalog lands: the class would go out unseen.
  const canSubmit = () =>
    name().trim() !== "" &&
    description().trim() !== "" &&
    classOptions().length > 0 &&
    !busy();
  const classHint = (): string =>
    errors().agent_class ??
    (classes.error
      ? agentErrorMessage(classes.error, "Could not load the agent classes.")
      : "Fixes its role, how it reports, and the sound of its callsign.");

  async function submit(): Promise<void> {
    if (!canSubmit()) return;
    setBusy(true);
    setErrors({});
    setFormError("");
    try {
      const agent = await createAgent({
        name: name().trim(),
        description: description().trim(),
        agent_class: shownClass(),
        endpoint_id: model().endpointId,
        model: model().model,
      });
      props.onCreated(agent.id);
    } catch (err) {
      const message = agentErrorMessage(err, "Could not create the agent.");
      const field = agentErrorField(err);
      if (
        field === "name" ||
        field === "description" ||
        field === "agent_class"
      ) {
        setErrors({ [field]: message });
      } else {
        setFormError(message);
      }
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal
      open={props.open}
      onClose={props.onClose}
      title="New agent"
      footer={
        <>
          <Button variant="ghost" onClick={props.onClose}>
            Cancel
          </Button>
          <Button
            variant="primary"
            leading="check"
            disabled={!canSubmit()}
            onClick={() => void submit()}
          >
            Create
          </Button>
        </>
      }
    >
      <Stack gap={3}>
        <Text variant="caption" tone="dim">
          A new agent starts as a draft. Publish it once its instructions and
          limits read right.
        </Text>
        <IdentityFields
          name={name()}
          description={description()}
          onNameInput={setName}
          onDescriptionInput={setDescription}
          namePattern={AGENT_NAME_PATTERN}
          patternHint="lowercase letters, digits, hyphens and underscores only"
          descriptionMax={AGENT_DESCRIPTION_MAX}
          namePlaceholder="code-reviewer"
          descriptionPlaceholder="What this agent is for and when to hand work to it."
          errors={errors()}
          disabled={busy()}
          autofocus
        />
        <Select
          label="Class"
          options={classOptions()}
          value={shownClass()}
          onChange={setAgentClass}
          invalid={Boolean(errors().agent_class || classes.error)}
          hint={classHint()}
          disabled={busy()}
        />
        <Stack gap={1}>
          <Text variant="caption">Model</Text>
          <ModelPicker value={model()} onChange={setModel} />
          <Text variant="caption" tone="dim">
            What it runs on wherever it is launched. The default is the
            sub-agent model, or the launching agent's own.
          </Text>
        </Stack>
        <Show when={formError()}>
          <Text variant="caption" tone="alert">
            {formError()}
          </Text>
        </Show>
      </Stack>
    </Modal>
  );
}
