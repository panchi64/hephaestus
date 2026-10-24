import { Show, splitProps, type JSX } from "solid-js";
import { cx } from "../cx";
import { Text } from "../primitives/Text";
import { Icon, type IconProps } from "../primitives/Icon";

export interface EmptyStateProps {
  /** Primary line. Defaults to "No data". */
  message?: string;
  /** Secondary dim hint. */
  hint?: string;
  icon?: IconProps["name"];
  /** Optional action (e.g. a Button). */
  action?: JSX.Element;
  class?: string;
}

/** "No data" placeholder for empty regions (§10 states). */
export function EmptyState(props: EmptyStateProps): JSX.Element {
  const [local] = splitProps(props, [
    "message",
    "hint",
    "icon",
    "action",
    "class",
  ]);
  return (
    <div
      class={cx(
        "flex flex-col items-center justify-center gap-2 px-4 py-8 text-center",
        local.class,
      )}
    >
      <Show when={local.icon}>
        <Icon name={local.icon!} size={24} class="text-dim" />
      </Show>
      <Text variant="label" tone="dim">
        {local.message ?? "No data"}
      </Text>
      <Show when={local.hint}>
        <Text variant="micro" tone="dim">
          {local.hint}
        </Text>
      </Show>
      <Show when={local.action}>
        <div class="mt-2">{local.action}</div>
      </Show>
    </div>
  );
}
