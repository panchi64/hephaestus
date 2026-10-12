import { createMemo, type JSX } from "solid-js";
import { Combobox } from "~/ui";
import {
  decodeModelValue,
  encodeModelValue,
  modelPickerGroups,
} from "~/lib/stores/models";

const THREAD_MODEL = "";

export interface ModelChoice {
  endpointId: string | null;
  model: string | null;
}

/** The model an agent runs on: one of the configured models, or the thread's
 *  own. The model is not part of an agent's file, so this is the one agent
 *  setting edited outside the document. */
export function ModelPicker(props: {
  value: ModelChoice;
  onChange: (choice: ModelChoice) => void;
}): JSX.Element {
  const value = () =>
    props.value.endpointId && props.value.model
      ? encodeModelValue({
          endpointId: props.value.endpointId,
          model: props.value.model,
        })
      : THREAD_MODEL;

  const groups = createMemo(() => {
    const all = [
      {
        label: "Default",
        options: [{ value: THREAD_MODEL, label: "Thread's model" }],
      },
      ...modelPickerGroups(),
    ];
    const current = value();
    const listed = all.some((g) => g.options.some((o) => o.value === current));
    if (current && !listed) {
      all.push({
        label: "Unavailable",
        options: [{ value: current, label: props.value.model ?? current }],
      });
    }
    return all;
  });

  function pick(next: string): void {
    const sel = next === THREAD_MODEL ? null : decodeModelValue(next);
    props.onChange({
      endpointId: sel?.endpointId ?? null,
      model: sel?.model ?? null,
    });
  }

  return (
    <Combobox
      groups={groups()}
      value={value()}
      onChange={pick}
      searchPlaceholder="Search models…"
      aria-label="Model"
    />
  );
}
