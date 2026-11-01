import { createSignal, For, Show, type JSX } from "solid-js";
import { Button, Chip, Combobox, Input, Row, Stack, Text, toast } from "~/ui";
import { isApiError } from "~/lib/api";
import { settled } from "~/lib/resource";
import { decodeModelValue, modelPickerGroups } from "~/lib/stores/models";
import { saveChoosableModels, useChoosableModels, useEndpoints } from "../data";
import { CHOOSABLE_NOTE_MAX, type ChoosableModel } from "../model";

type Entry = Pick<ChoosableModel, "endpointId" | "model" | "note">;

/** The models Talos may pick for an agent it spawns that has no pin. Empty by default,
 *  so only a model the operator listed here can ever be chosen; the note tells Talos
 *  what each one is for. */
export function ChoosableModels(): JSX.Element {
  const list = useChoosableModels();
  const endpoints = useEndpoints();
  const [picked, setPicked] = createSignal("");
  const [note, setNote] = createSignal("");

  const entries = (): ChoosableModel[] => settled(list) ?? [];
  const endpointName = (id: string): string =>
    (settled(endpoints) ?? []).find((e) => e.id === id)?.name ?? "missing";

  const save = async (next: Entry[], done: string): Promise<boolean> => {
    try {
      await saveChoosableModels(next);
      toast.success(done);
      return true;
    } catch (e) {
      toast.error(isApiError(e) ? e.detail : "Unable to save the list.");
      return false;
    }
  };

  const add = async (): Promise<void> => {
    const sel = decodeModelValue(picked());
    if (!sel) return;
    const entry = {
      endpointId: sel.endpointId,
      model: sel.model,
      note: note().trim(),
    };
    if (
      await save([...entries(), entry], `Talos may now choose ${sel.model}.`)
    ) {
      setPicked("");
      setNote("");
    }
  };

  const remove = (target: ChoosableModel): Promise<boolean> =>
    save(
      entries().filter((e) => e !== target),
      `${target.model} is off the list.`,
    );

  const editNote = (target: ChoosableModel, text: string): void => {
    const trimmed = text.trim();
    if (trimmed === target.note) return;
    void save(
      entries().map((e) => (e === target ? { ...e, note: trimmed } : e)),
      "Note saved.",
    );
  };

  return (
    <Stack gap={2}>
      <Text variant="label" tone="dim">
        Models agents may choose
      </Text>
      <Text variant="caption" tone="dim">
        When it spawns an agent that has no model of its own, Talos may pick one
        of these by its note. With none listed, every spawned agent runs on its
        default.
      </Text>
      <For each={entries()}>
        {(entry) => (
          <Row gap={2} align="center" class="flex-wrap">
            <Text variant="caption" tone={entry.available ? "bright" : "dim"}>
              {endpointName(entry.endpointId)} / {entry.model}
            </Text>
            <Chip>{entry.local ? "Local" : "Hosted"}</Chip>
            <Show when={!entry.available}>
              <Text variant="micro" tone="alert">
                unavailable
              </Text>
            </Show>
            <Input
              fieldClass="min-w-48 flex-1"
              value={entry.note}
              maxLength={CHOOSABLE_NOTE_MAX}
              placeholder="What it is for"
              aria-label={`Note for ${entry.model}`}
              onChange={(e) => editNote(entry, e.currentTarget.value)}
            />
            <Button
              variant="ghost"
              leading="trash"
              aria-label={`Remove ${entry.model}`}
              onClick={() => void remove(entry)}
            />
          </Row>
        )}
      </For>
      <Row gap={2} align="center" class="flex-wrap">
        <Combobox
          groups={modelPickerGroups()}
          value={picked()}
          onChange={setPicked}
          placeholder="Add a model"
          searchPlaceholder="Search models…"
          aria-label="Model agents may choose"
        />
        <Input
          fieldClass="min-w-48 flex-1"
          value={note()}
          maxLength={CHOOSABLE_NOTE_MAX}
          placeholder="What it is for, e.g. fast local file sweeps"
          aria-label="Note for the new model"
          onInput={(e) => setNote(e.currentTarget.value)}
        />
        <Button leading="plus" disabled={!picked()} onClick={() => void add()}>
          Add
        </Button>
      </Row>
    </Stack>
  );
}
