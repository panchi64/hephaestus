import { Show, splitProps, type JSX } from "solid-js";
import { cx } from "../cx";
import { Text } from "../primitives/Text";

export interface ToggleProps {
  checked?: boolean;
  onChange?: (checked: boolean) => void;
  label?: string;
  disabled?: boolean;
  class?: string;
}

/** Mechanical two-state switch: a square thumb snaps across a hairline track.
 *  No sliding flourish — the thumb position is instant. */
export function Toggle(props: ToggleProps): JSX.Element {
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
      <button
        type="button"
        role="switch"
        aria-checked={local.checked ?? false}
        disabled={local.disabled}
        onClick={() => local.onChange?.(!local.checked)}
        class={cx(
          "relative h-4 w-7 border transition-colors",
          local.checked ? "border-bright bg-raised" : "border-line bg-surface",
        )}
      >
        <span
          class={cx(
            "absolute top-0.5 size-2.5 transition-[left]",
            local.checked ? "left-[14px] bg-bright" : "left-0.5 bg-dim",
          )}
          aria-hidden="true"
        />
      </button>
      <Show when={local.label}>
        <Text variant="label" tone="default">
          {local.label}
        </Text>
      </Show>
    </label>
  );
}
