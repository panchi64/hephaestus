import { createEffect, createSignal, Show, type JSX } from "solid-js";
import { Button, Modal, Stack, Text, Textarea } from "~/ui";
import {
  createWorkflow,
  workflowErrorField,
  workflowErrorMessage,
} from "../data";
import { charCount, WORKFLOW_BODY_MAX } from "../model";
import { WorkflowFields, type WorkflowFieldErrors } from "./WorkflowFields";

/**
 * Create a workflow from the two things it cannot exist without: the name it is typed
 * by, and the template that name stands for.
 *
 * The picker copy — title, description, argument hint — is asked for in the editor
 * instead, because it is far easier to write a one-line description of a template that
 * is already on screen than of one still in your head.
 */
export function NewWorkflowDialog(props: {
  open: boolean;
  onClose: () => void;
  onCreated: (id: string) => void;
}): JSX.Element {
  const [name, setName] = createSignal("");
  const [body, setBody] = createSignal("");
  const [errors, setErrors] = createSignal<WorkflowFieldErrors>({});
  const [formError, setFormError] = createSignal("");
  const [busy, setBusy] = createSignal(false);

  // Reset each time the dialog opens, so a previous rejection doesn't linger.
  createEffect(() => {
    if (!props.open) return;
    setName("");
    setBody("");
    setErrors({});
    setFormError("");
  });

  const canSubmit = () =>
    name().trim() !== "" && body().trim() !== "" && !busy();

  async function submit(): Promise<void> {
    if (!canSubmit()) return;
    setBusy(true);
    setErrors({});
    setFormError("");
    try {
      const workflow = await createWorkflow(name().trim(), body().trim());
      props.onCreated(workflow.id);
    } catch (err) {
      const message = workflowErrorMessage(
        err,
        "Could not create the workflow.",
      );
      const field = workflowErrorField(err);
      if (field === "name" || field === "body") {
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
      title="New workflow"
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
          Typing /NAME in the composer puts this template in front of the model
          for that turn. Only you can reach it — the agent is never shown the
          list.
        </Text>
        <WorkflowFields
          compact
          name={name()}
          title=""
          description=""
          argumentHint=""
          onNameInput={setName}
          onTitleInput={() => {}}
          onDescriptionInput={() => {}}
          onArgumentHintInput={() => {}}
          errors={errors()}
          disabled={busy()}
          autofocus
        />
        <Textarea
          label="Template"
          value={body()}
          disabled={busy()}
          invalid={Boolean(errors().body) || body().length > WORKFLOW_BODY_MAX}
          hint={errors().body ?? charCount(body(), WORKFLOW_BODY_MAX)}
          placeholder="Summarise what changed since yesterday, grouped by area."
          rows={6}
          onInput={(e) => setBody(e.currentTarget.value)}
        />
        {/* A rejection that named no field — shown verbatim, same as the rest. */}
        <Show when={formError()}>
          <Text variant="caption" tone="alert">
            {formError()}
          </Text>
        </Show>
      </Stack>
    </Modal>
  );
}
