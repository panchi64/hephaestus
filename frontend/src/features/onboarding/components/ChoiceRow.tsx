import { createSignal, type JSX } from "solid-js";
import { Select } from "~/ui";
import { SettingRow } from "~/features/settings/components/SettingRow";

export interface RowOption {
  id: string;
  title: string;
  reason: string;
  /** Leads the reason line in the menu (`Recommended`, the endpoint's name). */
  hint?: string;
}

/** The value a row reads when it is on its default. */
export const DEFAULT_PICK = "default";

/** One setting on one line: what it is for, and a dropdown showing its current pick.
 *  The menu lists the default first, saying what it means, then each option with its
 *  reason. A pick is written at once. */
export function ChoiceRow(props: {
  label: string;
  purpose: JSX.Element;
  defaultTitle: string;
  defaultMeaning: string;
  /** The picked option's id, or `DEFAULT_PICK`. */
  value: string;
  options: RowOption[];
  onPick: (id: string) => Promise<void>;
}): JSX.Element {
  const [busy, setBusy] = createSignal(false);

  const options = () => [
    {
      value: DEFAULT_PICK,
      label: props.defaultTitle,
      description: `Default · ${props.defaultMeaning}`,
    },
    ...props.options.map((o) => ({
      value: o.id,
      label: o.title,
      description: [o.hint, o.reason].filter(Boolean).join(" · "),
    })),
  ];

  const pick = async (id: string): Promise<void> => {
    if (id === props.value) return;
    setBusy(true);
    try {
      await props.onPick(id);
    } finally {
      setBusy(false);
    }
  };

  return (
    <SettingRow
      label={props.label}
      description={props.purpose}
      class="gap-4 px-3 py-3"
    >
      <Select
        class="w-64 shrink-0"
        aria-label={props.label}
        options={options()}
        value={props.value}
        onChange={(id) => void pick(id)}
        disabled={busy()}
      />
    </SettingRow>
  );
}
