import { createEffect, createSignal, Show, type JSX } from "solid-js";
import { Button, Input, Modal, Stack, Text, Textarea } from "~/ui";
import { agentErrorField, agentErrorMessage, createAgent } from "../data";
import { AGENT_NAME_PATTERN } from "../model";

type Errors = Partial<Record<"name" | "description", string>>;

/** Create an agent from the two fields that identify it. Everything else is
 *  set in the editor; a new agent starts as a draft. */
export function NewAgentDialog(props: {
  open: boolean;
  onClose: () => void;
  onCreated: (id: string) => void;
}): JSX.Element {
  const [name, setName] = createSignal("");
  const [description, setDescription] = createSignal("");
  const [errors, setErrors] = createSignal<Errors>({});
  const [formError, setFormError] = createSignal("");
  const [busy, setBusy] = createSignal(false);

  createEffect(() => {
    if (!props.open) return;
    setName("");
    setDescription("");
    setErrors({});
    setFormError("");
  });

  const malformed = () => name() !== "" && !AGENT_NAME_PATTERN.test(name());
  const canSubmit = () =>
    name().trim() !== "" && description().trim() !== "" && !busy();

  async function submit(): Promise<void> {
    if (!canSubmit()) return;
    setBusy(true);
    setErrors({});
    setFormError("");
    try {
      const agent = await createAgent({
        name: name().trim(),
        description: description().trim(),
      });
      props.onCreated(agent.id);
    } catch (err) {
      const message = agentErrorMessage(err, "Could not create the agent.");
      const field = agentErrorField(err);
      if (field === "name" || field === "description") {
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
        <Text variant="micro" tone="dim">
          A new agent starts as a draft. Publish it once its instructions and
          limits read right.
        </Text>
        <Input
          label="Name"
          value={name()}
          autofocus
          disabled={busy()}
          invalid={Boolean(errors().name) || malformed()}
          hint={
            errors().name ??
            (malformed()
              ? "Lowercase letters, digits, hyphens and underscores only"
              : undefined)
          }
          placeholder="code-reviewer"
          onInput={(e) => setName(e.currentTarget.value)}
        />
        <Textarea
          label="Description"
          value={description()}
          disabled={busy()}
          invalid={Boolean(errors().description)}
          hint={errors().description}
          placeholder="What this agent is for and when to hand work to it."
          rows={3}
          onInput={(e) => setDescription(e.currentTarget.value)}
        />
        <Show when={formError()}>
          <Text variant="micro" tone="alert">
            {formError()}
          </Text>
        </Show>
      </Stack>
    </Modal>
  );
}
