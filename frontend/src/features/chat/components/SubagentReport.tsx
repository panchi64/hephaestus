import { Show, type JSX } from "solid-js";
import { Collapse, Markdown, Text } from "~/ui";
import { markdownPreview } from "../markdownPreview";
import type { ChatMessage } from "../model";
import {
  ProcessRow,
  SchematicContext,
  Sep,
  createAdoptedOpen,
} from "./ProcessRow";

/**
 * A report or message from another agent, where the thread received it.
 *
 * It arrives as a request message, but it isn't the operator's words, so it is neither a
 * bubble nor full prose: one inbound row in the work log's schematic voice, the mirror of
 * the outbound `agents_message` call, resting closed with the text one click away. No
 * actions: edit, fork and retry act on what the operator said, and the sender may already
 * have ended.
 */
export function SubagentReport(props: {
  message: ChatMessage;
  /** The sending agent's callsign, when the thread's sub-agent list knows it. */
  callsign?: string;
}): JSX.Element {
  const { open, toggle } = createAdoptedOpen({});
  const plate = (): string => {
    const { author, reporter } = props.message;
    if (author) return `Message · ${props.callsign || author}`;
    return `Report · ${props.callsign || reporter || "sub-agent"}`;
  };
  return (
    <div class="my-1 w-full">
      {/* The schematic is what draws the framed inbound socket. */}
      <SchematicContext.Provider value={true}>
        <ProcessRow
          open={open()}
          onToggle={toggle}
          icon="users"
          frame="in"
          label={plate()}
        >
          <Show when={markdownPreview(props.message.content)}>
            {(line) => (
              <>
                <Sep />
                <Text variant="micro" tone="dim" class="min-w-0 truncate">
                  {line()}
                </Text>
              </>
            )}
          </Show>
        </ProcessRow>
      </SchematicContext.Provider>
      <Collapse open={open()}>
        <div class="px-2 py-1.5">
          <Markdown>{props.message.content}</Markdown>
        </div>
      </Collapse>
    </div>
  );
}
