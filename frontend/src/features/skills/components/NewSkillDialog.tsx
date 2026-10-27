import { createEffect, createSignal, Show, type JSX } from "solid-js";
import { Button, Modal, Stack, Text } from "~/ui";
import { createSkill, skillErrorField, skillErrorMessage } from "../data";
import {
  SkillIdentityFields,
  type SkillFieldErrors,
} from "./SkillIdentityFields";

/** Create a skill from its two required fields. The body is written afterwards
 *  in the editor, so this dialog only asks for what the backend requires up
 *  front — and shows whatever it rejects, on the field it named. */
export function NewSkillDialog(props: {
  open: boolean;
  onClose: () => void;
  onCreated: (id: string) => void;
}): JSX.Element {
  const [name, setName] = createSignal("");
  const [description, setDescription] = createSignal("");
  const [errors, setErrors] = createSignal<SkillFieldErrors>({});
  const [formError, setFormError] = createSignal("");
  const [busy, setBusy] = createSignal(false);

  // Reset each time the dialog opens, so a previous rejection doesn't linger.
  createEffect(() => {
    if (!props.open) return;
    setName("");
    setDescription("");
    setErrors({});
    setFormError("");
  });

  const canSubmit = () =>
    name().trim() !== "" && description().trim() !== "" && !busy();

  async function submit(): Promise<void> {
    if (!canSubmit()) return;
    setBusy(true);
    setErrors({});
    setFormError("");
    try {
      const skill = await createSkill(name().trim(), description().trim());
      props.onCreated(skill.id);
    } catch (err) {
      const message = skillErrorMessage(err, "Could not create the skill.");
      const field = skillErrorField(err);
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
      title="New skill"
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
          The agent matches on DESCRIPTION — say what the skill does and when to
          reach for it. A new skill starts as a draft.
        </Text>
        <SkillIdentityFields
          name={name()}
          description={description()}
          onNameInput={setName}
          onDescriptionInput={setDescription}
          errors={errors()}
          disabled={busy()}
          autofocus
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
