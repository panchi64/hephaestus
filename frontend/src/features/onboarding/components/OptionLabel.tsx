import { type JSX } from "solid-js";
import { Text } from "~/ui";

/** A choice row's label with its reason under it: what the option is, then why you'd
 *  pick it, so a row can be chosen without knowing the platform. */
export function OptionLabel(props: {
  title: JSX.Element;
  reason: string;
}): JSX.Element {
  return (
    <span class="flex min-w-0 flex-col gap-0.5">
      <span>{props.title}</span>
      <Text variant="micro" tone="dim" class="font-sans normal-case">
        {props.reason}
      </Text>
    </span>
  );
}
