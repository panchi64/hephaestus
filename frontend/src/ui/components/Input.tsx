import { Show, splitProps, type JSX } from "solid-js";
import { cx } from "../cx";
import { Text } from "../primitives/Text";
import { Icon } from "../primitives/Icon";
import { type IconName } from "../icons/registry";

export interface InputProps extends JSX.InputHTMLAttributes<HTMLInputElement> {
  /** Uppercase field label rendered above the control. */
  label?: string;
  /** Alert/validation state. */
  invalid?: boolean;
  /** Dim helper or error text below the control. */
  hint?: string;
  /** Leading glyph rendered inside the control (e.g. `search`). */
  leading?: IconName;
  /** Class for the wrapping label, which is the flex item; `class` targets the inner input. */
  fieldClass?: string;
}

const fieldClass =
  /* A field the operator types into is the interface's, so it is sans (§2) —
     the old mono made every form read as a terminal prompt. The resting look is
     a filled surface with a hairline; focus is the neutral ring from theme.css,
     which sits outside the box and so shifts nothing. */
  "w-full bg-raised border px-3 h-8 text-body font-sans text-bright placeholder:text-dim outline-none transition-colors disabled:opacity-40 disabled:cursor-not-allowed";

/** Single-line text control. */
export function Input(props: InputProps): JSX.Element {
  const [local, rest] = splitProps(props, [
    "label",
    "invalid",
    "hint",
    "leading",
    "class",
    "fieldClass",
  ]);
  return (
    <label class={cx("flex flex-col gap-1", local.fieldClass)}>
      <Show when={local.label}>
        <Text variant="label" tone="dim">
          {local.label}
        </Text>
      </Show>
      <div class="relative">
        <Show when={local.leading}>
          <Icon
            name={local.leading!}
            size={12}
            class="pointer-events-none absolute left-2 top-1/2 -translate-y-1/2 text-dim"
          />
        </Show>
        <input
          class={cx(
            fieldClass,
            local.leading && "pl-8",
            local.invalid ? "border-alert" : "border-transparent",
            local.class,
          )}
          aria-invalid={local.invalid || undefined}
          {...rest}
        />
      </div>
      <Show when={local.hint}>
        <Text variant="caption" tone={local.invalid ? "alert" : "dim"}>
          {local.hint}
        </Text>
      </Show>
    </label>
  );
}

export { fieldClass };
