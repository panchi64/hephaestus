import { type JSX } from "solid-js";
import { Stack, Text } from "~/ui";
import type { ChatMessage } from "../model";

/**
 * A sub-agent's report, where the thread received it.
 *
 * It arrives as a request message because that is the one shape a model has for anything
 * said to it from outside, and left alone it renders as a bubble on the operator's own
 * side of the thread — words attributed to them that they have not read and were never
 * asked about. So it is neither a bubble nor a panel: an inset note, set apart from both
 * sides, saying who reported and what they said.
 *
 * **An inset note and not the compaction turn's console group**, though both are turns
 * nobody in the thread took. The difference is what they hold: a fold's checkpoint is a
 * machine readout with a fixed roster of sections, scanned rather than read, which is what
 * §10.14 keeps a frame for. This is one agent's prose report to another — ordinary content
 * at ordinary density, and a frame around that is the box §7 spends its length refusing.
 *
 * **No actions, deliberately.** Everything a turn usually offers — edit, fork, rewind,
 * retry — acts on something the operator said, and there is nothing here to send back to:
 * the sub-agent that wrote it has already ended. The report is also not the place to read
 * the work; the card in the Agents panel opens the whole transcript.
 */
export function SubagentReport(props: { message: ChatMessage }): JSX.Element {
  return (
    // A plate naming the region with a hairline running on from it, then the prose
    // bare — the console register's way of setting something apart without boxing it
    // (design §7). The 2px left rule this replaced is the emphasis border §7 retired.
    <Stack gap={2} class="my-1 w-full">
      <div class="flex items-center gap-3">
        <Text variant="plate" tone="dim" class="shrink-0">
          {props.message.author
            ? `Message · ${props.message.author}`
            : "Report · sub-agent"}
        </Text>
        <span class="h-px flex-1 bg-line" aria-hidden="true" />
      </div>
      <Text variant="body" tone="default" class="whitespace-pre-wrap">
        {props.message.content}
      </Text>
    </Stack>
  );
}
