import { For, Show, type JSX } from "solid-js";
import { Button, Text, Textarea, cx } from "~/ui";
import {
  idleReason,
  isOperatorItem,
  waitingLabel,
  type InboxItem,
  type InboxState,
} from "../inbox";
import { createQueuedEdit } from "../queuedEdit";
import type { InboxActions } from "./InboxDock";

/**
 * **What is queued behind the running turn, at the foot of the thread** where it will
 * land.
 *
 * Dimmed, because none of it has reached the model: the operator's own messages as
 * bubbles marked QUEUED, and everything else as one compact line. A row leaves when its
 * message is injected and the real row arrives through the stream, so nothing here is a
 * second copy of a delivered message.
 */
export function InboxRows(
  props: { inbox: InboxState } & InboxActions,
): JSX.Element {
  return (
    <For each={props.inbox.items}>
      {(item) => (
        <Show
          when={isOperatorItem(item)}
          fallback={<WaitingLine inbox={props.inbox} item={item} />}
        >
          <QueuedBubble inbox={props.inbox} item={item} actions={props} />
        </Show>
      )}
    </For>
  );
}

function WaitingLine(props: {
  inbox: InboxState;
  item: InboxItem;
}): JSX.Element {
  return (
    <div class="flex items-center gap-2 px-4 py-1 opacity-70">
      <Text variant="meta" tone="dim" class="min-w-0 truncate">
        Waiting · {waitingLabel(props.item)}
      </Text>
      <Show when={idleReason(props.inbox, props.item)}>
        {(reason) => (
          <Text variant="meta" tone="dim" class="shrink-0">
            · {reason()}
          </Text>
        )}
      </Show>
    </div>
  );
}

function QueuedBubble(props: {
  inbox: InboxState;
  item: InboxItem;
  actions: InboxActions;
}): JSX.Element {
  const edit = createQueuedEdit({
    messageId: () => props.item.id,
    queued: () => true,
    initial: () => props.item.text,
    onHold: (held) => props.actions.onHold?.(props.item.id, held),
    onSave: (text) => props.actions.onEdit?.(props.item.id, text),
  });
  return (
    <div class="flex flex-col items-end gap-1 px-4 py-3">
      <div
        class={cx(
          "border border-line-strong bg-raised",
          edit.editing()
            ? "w-full max-w-[78%]"
            : "w-fit max-w-[78%] opacity-60",
        )}
      >
        <div class="flex items-center justify-end gap-2 px-2.5 pt-[7px]">
          <Show when={!edit.editing()}>
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
              onClick={() => props.actions.onWithdraw?.(props.item.id)}
            >
              Withdraw
            </Button>
          </Show>
          <Text variant="meta" tone="warn">
            Queued
          </Text>
          <Show when={props.item.turn}>
            <Text variant="meta" tone="dim">
              After this turn
            </Text>
          </Show>
          {/* A stalled queue says why; silence reads as the agent having stalled. */}
          <Show when={idleReason(props.inbox, props.item)}>
            {(reason) => (
              <Text variant="meta" tone="dim">
                {reason()}
              </Text>
            )}
          </Show>
        </div>
        <Show
          when={edit.editing()}
          fallback={
            <Text
              variant="body"
              tone="bright"
              class="block px-3.5 pt-1.5 pb-[11px] whitespace-pre-wrap"
            >
              {props.item.text}
            </Text>
          }
        >
          <div class="px-3.5 pt-1.5 pb-[11px]">
            <Textarea
              value={edit.draft()}
              rows={3}
              onInput={(e) => edit.write(e.currentTarget.value)}
              aria-label="Edit queued message"
            />
            <div class="mt-1 flex justify-end gap-2">
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
            </div>
          </div>
        </Show>
      </div>
    </div>
  );
}
