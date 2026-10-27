import { Show, splitProps, type JSX } from "solid-js";
import { cx } from "../cx";
import { Text } from "../primitives/Text";
import { Icon } from "../primitives/Icon";
import { Button } from "./Button";

export interface ErrorStateProps {
  /** Primary line. Defaults to "Something went wrong". */
  message?: string;
  /** Secondary detail — surface the actual error reason here. */
  hint?: string;
  /** Shows a RETRY button when provided. */
  onRetry?: () => void;
  retryLabel?: string;
  class?: string;
}

/** Alert-toned failure placeholder with an optional retry — the error sibling
 *  to LoadingText / EmptyState (§6 states). Use for failed resources/actions. */
export function ErrorState(props: ErrorStateProps): JSX.Element {
  const [local] = splitProps(props, [
    "message",
    "hint",
    "onRetry",
    "retryLabel",
    "class",
  ]);
  return (
    <div
      class={cx(
        "flex flex-col items-center justify-center gap-2 px-4 py-8 text-center",
        local.class,
      )}
    >
      <Icon name="warning" size={24} class="text-alert" />
      <Text variant="label" tone="alert">
        {local.message ?? "Something went wrong"}
      </Text>
      <Show when={local.hint}>
        <Text variant="caption" tone="dim">
          {local.hint}
        </Text>
      </Show>
      <Show when={local.onRetry}>
        <Button
          variant="default"
          size="sm"
          leading="refresh"
          class="mt-2"
          onClick={() => local.onRetry!()}
        >
          {local.retryLabel ?? "Retry"}
        </Button>
      </Show>
    </div>
  );
}
