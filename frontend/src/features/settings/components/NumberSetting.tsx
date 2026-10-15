import { createEffect, createSignal, type JSX } from "solid-js";
import { Button, Input, Row, Stack, Text, toast } from "~/ui";

/** A whole number within `[min, max]`, or null when the field can't supply one.
 *  `Number("")` is 0, so a blanked field has to be rejected explicitly rather than read
 *  as zero. */
export function wholeNumber(
  raw: string,
  { min = 1, max = Number.MAX_SAFE_INTEGER } = {},
): number | null {
  const n = Number(raw.trim());
  if (raw.trim() === "" || !Number.isInteger(n) || n < min || n > max)
    return null;
  return n;
}

/** One preference's title and its explanation — the shape every block in a settings
 *  panel opens with. */
export function SettingHeader(props: {
  title: string;
  children: JSX.Element;
}): JSX.Element {
  return (
    <Stack gap={1}>
      <Text variant="label" tone="default">
        {props.title}
      </Text>
      <Text variant="micro" tone="dim">
        {props.children}
      </Text>
    </Stack>
  );
}

/** A whole-number preference with its own Save: seeded from what is stored, checked
 *  against its bounds, and reseeded from what the backend kept. */
export function NumberSetting(props: {
  title: string;
  children: JSX.Element;
  /** What is stored now; null shows the placeholder. */
  stored: number | null;
  placeholder: string;
  min?: number;
  max?: number;
  /** What the operator is told when the field holds no number in range. */
  invalid: string;
  /** The thing being set, for the success and failure toasts. */
  noun: string;
  save: (value: number) => Promise<number | null>;
}): JSX.Element {
  const [draft, setDraft] = createSignal("");
  const [saving, setSaving] = createSignal(false);
  createEffect(() =>
    setDraft(props.stored === null ? "" : String(props.stored)),
  );
  const save = async () => {
    const n = wholeNumber(draft(), { min: props.min ?? 1, max: props.max });
    if (n === null) {
      toast.error(props.invalid);
      return;
    }
    setSaving(true);
    try {
      const saved = await props.save(n);
      setDraft(saved === null ? "" : String(saved));
      toast.success(`${props.noun} updated`);
    } catch {
      toast.error(`Unable to update the ${props.noun.toLowerCase()}.`);
    } finally {
      setSaving(false);
    }
  };
  return (
    <>
      <SettingHeader title={props.title}>{props.children}</SettingHeader>
      <Row gap={2} align="center">
        <div class="w-48">
          <Input
            type="number"
            inputMode="numeric"
            min={String(props.min ?? 1)}
            max={props.max === undefined ? undefined : String(props.max)}
            value={draft()}
            onInput={(e) => setDraft(e.currentTarget.value)}
            placeholder={props.placeholder}
          />
        </div>
        <Button
          variant="primary"
          disabled={saving()}
          onClick={() => void save()}
        >
          {saving() ? "Saving…" : "Save"}
        </Button>
      </Row>
    </>
  );
}
