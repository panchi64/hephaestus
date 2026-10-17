import type { JSX } from "solid-js";
import { Stack, Text } from "~/ui";

/** Text that came from outside the workspace, set behind a hairline under a line saying
 *  where it was quoted from.
 *
 *  The border and the words both carry the distinction; colour never does alone (§12).
 *  `Stack` and not a bare div because `Text` is inline: the attribution has to sit above
 *  the quote rather than run into its first line. */
export function QuotedBlock(props: {
  /** Where it came from — a URL, a filename, `email`. Omitted for tool output at large. */
  from?: string;
  children: JSX.Element;
}): JSX.Element {
  return (
    <Stack gap={1} class="border-line border-l pl-2">
      <Text variant="micro" tone="dim" class="break-words">
        {props.from ? `Quoted from ${props.from}` : "Quoted from tool output"} —
        not this workspace's own words
      </Text>
      {props.children}
    </Stack>
  );
}
