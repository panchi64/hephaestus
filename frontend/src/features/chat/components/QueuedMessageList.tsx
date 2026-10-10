import { For, Show, type JSX } from "solid-js";
import { Button, Row, Stack, Text, Textarea } from "~/ui";
import type { ChatMessage } from "../model";
import { createQueuedEdit } from "../queuedEdit";

/**
 * **What is already waiting to be sent**, listed inside the dock that has taken the
 * composer's slot.
 *
 * A park and a queued message can be outstanding at the same moment, and that pairing is
 * where the surface used to fail: the dock replaces the composer, so the bubbles' own
 * edit and withdraw controls are above the fold of a transcript the operator cannot
 * reach past the panel — and a message written a minute ago may be exactly what the
 * agent has now stopped to ask about. Both reach the model on the same resume. Listing
 * them here says so, and makes the queue answerable from the surface that is holding the
 * operator's attention.
 *
 * Compact on purpose: this is context for the answer, not the answer. The full bubble
 * with its timestamp and identity row belongs in the transcript; what is needed here is
 * the words, and a way to change or take back the ones that are wrong.
 */
export function QueuedMessageList(props: {
  messages: ChatMessage[];
  onEdit?: (queuedMessageId: string, text: string) => void;
  onWithdraw?: (queuedMessageId: string) => void;
  onHold?: (queuedMessageId: string, held: boolean) => void;
}): JSX.Element {
  return (
    // Flush ruled rows under a plate: it sits at the top of the dock's body, which pads
    // nothing (design §10.18), and the rows rule themselves off the ask below.
    <div class="flex flex-col border-b border-line">
      <Text variant="plate" tone="dim" class="px-3 pt-3 pb-2">
        Waiting to send · {props.messages.length}
      </Text>
      <For each={props.messages}>
        {(message) => (
          <QueuedMessageRow
            message={message}
            onEdit={props.onEdit}
            onWithdraw={props.onWithdraw}
            onHold={props.onHold}
          />
        )}
      </For>
    </div>
  );
}

function QueuedMessageRow(props: {
  message: ChatMessage;
  onEdit?: (queuedMessageId: string, text: string) => void;
  onWithdraw?: (queuedMessageId: string) => void;
  onHold?: (queuedMessageId: string, held: boolean) => void;
}): JSX.Element {
  const m = () => props.message;
  const id = () => m().queuedMessageId;
  // The same hold-and-draft protocol the transcript bubble follows, and the reason it is
  // a module rather than a copy: two surfaces editing one queued message must not differ
  // on when the run is told to stop draining it.
  const edit = createQueuedEdit({
    messageId: id,
    queued: () => m().queuedPending === true,
    initial: () => m().content,
    onHold: (held) => {
      const at = id();
      if (at) props.onHold?.(at, held);
    },
    onSave: (text) => {
      const at = id();
      if (at) props.onEdit?.(at, text);
    },
  });

  return (
    <Stack gap={1} class="border-t border-line px-3 py-2">
      <Show
        when={edit.editing()}
        fallback={
          <Row justify="between" align="start" gap={2}>
            <Text variant="body" tone="dim" class="min-w-0 flex-1">
              {m().content}
            </Text>
            <Row gap={1} align="center">
              <Show when={m().queuedHeld}>
                <Text variant="meta" tone="dim">
                  Held
                </Text>
              </Show>
              <Button
                variant="ghost"
                size="sm"
                leading="pen"
                aria-label="Edit queued message"
                onClick={edit.start}
              >
                Edit
              </Button>
              <Button
                variant="ghost"
                size="sm"
                leading="close"
                aria-label="Withdraw queued message"
                onClick={() => {
                  const at = id();
                  if (at) props.onWithdraw?.(at);
                }}
              >
                Withdraw
              </Button>
            </Row>
          </Row>
        }
      >
        <Textarea
          value={edit.draft()}
          rows={2}
          onInput={(e) => edit.write(e.currentTarget.value)}
          aria-label="Edit queued message"
        />
        <Row justify="end" gap={2}>
          <Button variant="ghost" size="sm" onClick={edit.cancel}>
            Cancel
          </Button>
          <Button
            variant="primary"
            size="sm"
            disabled={!edit.draft().trim()}
            onClick={edit.save}
          >
            Save
          </Button>
        </Row>
      </Show>
    </Stack>
  );
}
