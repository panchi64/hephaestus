import { createEffect, createSignal, type JSX } from "solid-js";
import { Button, Modal, Stack } from "~/ui";
import type { ModelEndpoint } from "~/lib/stores/models";
import { useEndpointEditor } from "../useEndpointEditor";
import { EndpointForm } from "./EndpointForm";
import { saveEndpointWithToast } from "./saveEndpoint";

/** Create/edit an endpoint in a modal; the caller says whether it's open and on which endpoint. */
export function EndpointFormDialog(props: {
  open: boolean;
  /** The endpoint being edited, or `null` to create a new one. */
  endpoint: ModelEndpoint | null;
  onClose: () => void;
}): JSX.Element {
  const editor = useEndpointEditor();

  // Seed the fields each time it opens, on whichever endpoint.
  createEffect(() => {
    if (props.open) editor.seed(props.endpoint);
  });

  const [saving, setSaving] = createSignal(false);
  const save = async () => {
    if (!editor.valid() || saving()) return;
    setSaving(true);
    try {
      const id = await saveEndpointWithToast(editor.target(), editor.values());
      if (id !== null) props.onClose();
    } finally {
      setSaving(false);
    }
  };

  return (
    <Modal
      open={props.open}
      onClose={props.onClose}
      title={props.endpoint ? "Edit endpoint" : "Add endpoint"}
      class="max-w-lg"
    >
      <Stack gap={3}>
        <EndpointForm
          editing={!!props.endpoint}
          values={editor.values()}
          providerOptions={editor.providerOptions()}
          keyHint={editor.keyHint()}
          onChange={editor.onChange}
        />
        <div class="flex justify-end gap-2">
          <Button variant="ghost" onClick={props.onClose}>
            Cancel
          </Button>
          <Button
            variant="primary"
            disabled={!editor.valid() || saving()}
            onClick={save}
          >
            {saving() ? "Saving…" : "Save"}
          </Button>
        </div>
      </Stack>
    </Modal>
  );
}
