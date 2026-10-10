import { Show, splitProps, type JSX } from "solid-js";
import { cx } from "../cx";
import { Text } from "../primitives/Text";

export interface TextareaProps extends JSX.TextareaHTMLAttributes<HTMLTextAreaElement> {
  label?: string;
  invalid?: boolean;
  hint?: string;
}

/** Multi-line text control, sharing the Input field treatment. */
export function Textarea(props: TextareaProps): JSX.Element {
  const [local, rest] = splitProps(props, [
    "label",
    "invalid",
    "hint",
    "class",
  ]);
  return (
    <label class="flex flex-col gap-1">
      <Show when={local.label}>
        <Text variant="label" tone="dim">
          {local.label}
        </Text>
      </Show>
      <textarea
        class={cx(
          "w-full resize-y bg-raised border border-transparent px-3 py-2 text-body font-sans text-bright placeholder:text-dim outline-none transition-colors disabled:opacity-40",
          local.invalid ? "border-alert" : "border-line",
          local.class,
        )}
        aria-invalid={local.invalid || undefined}
        {...rest}
      />
      <Show when={local.hint}>
        <Text variant="micro" tone={local.invalid ? "alert" : "dim"}>
          {local.hint}
        </Text>
      </Show>
    </label>
  );
}
