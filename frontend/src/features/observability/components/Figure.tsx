import { Show, type JSX } from "solid-js";
import { InfoHint, Text } from "~/ui";

/** One headline number with its label, an optional sub-line, and its explanation on
 *  hover rather than in the body. */
export function Figure(props: {
  label: string;
  value: JSX.Element;
  sub?: JSX.Element;
  hint: string;
}): JSX.Element {
  return (
    <div class="flex min-w-0 flex-col gap-1 border border-line p-3">
      <div class="flex items-center gap-1.5">
        <Text variant="meta" tone="dim">
          {props.label}
        </Text>
        <InfoHint label={props.hint} size={11} />
      </div>
      <Text variant="readout" tone="bright" class="tabular-nums">
        {props.value}
      </Text>
      <Show when={props.sub}>
        <Text variant="micro" tone="dim" class="tabular-nums">
          {props.sub}
        </Text>
      </Show>
    </div>
  );
}
