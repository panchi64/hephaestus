import { Show, splitProps, type JSX } from "solid-js";
import { cx } from "../cx";
import { Text } from "../primitives/Text";
import { Icon } from "../primitives/Icon";

export interface CheckboxProps {
  checked?: boolean;
  onChange?: (checked: boolean) => void;
  label?: string;
  disabled?: boolean;
  class?: string;
}

/** Square checkbox with a registry check glyph. Native input drives a11y. */
export function Checkbox(props: CheckboxProps): JSX.Element {
  const [local] = splitProps(props, [
    "checked",
    "onChange",
    "label",
    "disabled",
    "class",
  ]);
  return (
    <label
      class={cx(
        "inline-flex items-center gap-2",
        local.disabled ? "cursor-not-allowed opacity-40" : "cursor-pointer",
        local.class,
      )}
    >
      <input
        type="checkbox"
        class="sr-only"
        checked={local.checked}
        disabled={local.disabled}
        onChange={(e) => local.onChange?.(e.currentTarget.checked)}
      />
      <span
        class={cx(
          "flex size-4 items-center justify-center border transition-colors",
          local.checked
            ? "border-bright text-bright"
            : "border-line text-transparent",
        )}
        aria-hidden="true"
      >
        <Show when={local.checked}>
          <Icon name="check" size={12} />
        </Show>
      </span>
      <Show when={local.label}>
        <Text variant="label" tone="default">
          {local.label}
        </Text>
      </Show>
    </label>
  );
}
