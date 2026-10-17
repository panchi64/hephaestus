import { For, Show, type JSX } from "solid-js";
import { Button, ConsoleGroup, Row, Stack, Text, Textarea } from "~/ui";
import {
  idleReason,
  isOperatorItem,
  waitingLabel,
  type InboxItem,
  type InboxState,
} from "../inbox";
import { createQueuedEdit } from "../queuedEdit";

/** What the operator can do to their own queued messages. Absent on a host that does
 *  not steer; a report or a notice never gets these either way. */
export interface InboxActions {
  onEdit?: (messageId: string, text: string) => void;
  onWithdraw?: (messageId: string) => void;
  onHold?: (messageId: string, held: boolean) => void;
  /** Release a Stop's hold on the inbox. */
  onResume?: () => void;
}

/**
 * **Everything waiting in the conversation's inbox**, listed where the operator can
 * answer for it.
 *
 * It opens from the composer's count chip and is also listed inside the park dock: the
 * dock replaces the composer, so the thread's queued rows are out of the operator's
 * reach, and a message written a minute ago may be what the agent stopped to ask about.
 *
 * The operator's own messages show their words with edit and withdraw. Everything else
 * (a report, an agent's message, a notice) shows what it is, with nothing to change.
 *
 * After a Stop the inbox waits for the operator, and the plate says so with the one
 * action that releases it.
 */
export function InboxDock(
  props: {
    inbox: InboxState;
    /** Draw the frame and header band. Off inside the park dock, which supplies both. */
    framed?: boolean;
  } & InboxActions,
): JSX.Element {
  const legend = (): string =>
    props.inbox.waiting ? "Waiting for you" : "Waiting to send";
  const rows = (): JSX.Element => (
    <For each={props.inbox.items}>
      {(item) => (
        <Show
          when={isOperatorItem(item)}
          fallback={<WaitingRow inbox={props.inbox} item={item} />}
        >
          <OperatorRow inbox={props.inbox} item={item} actions={props} />
        </Show>
      )}
    </For>
  );
  const resume = (): JSX.Element => (
    <Show when={props.inbox.waiting && props.onResume}>
      <Button variant="default" size="sm" onClick={() => props.onResume?.()}>
        Resume
      </Button>
    </Show>
  );
  return (
    <Show
      when={props.framed}
      fallback={
        <div class="flex flex-col border-b border-line">
          <Row justify="between" align="center" class="px-3 pt-3 pb-2">
            <Text variant="plate" tone="dim">
              {legend()} · {props.inbox.items.length}
            </Text>
            {resume()}
          </Row>
          {rows()}
        </div>
      }
    >
      <ConsoleGroup
        label={`${legend()} · ${props.inbox.items.length}`}
        right={resume()}
        flush
        class="mb-2"
      >
        {rows()}
      </ConsoleGroup>
    </Show>
  );
}

function WaitingRow(props: {
  inbox: InboxState;
  item: InboxItem;
}): JSX.Element {
  return (
    <Row
      justify="between"
      align="start"
      gap={2}
      class="border-t border-line px-3 py-2"
    >
      <Text variant="meta" tone="dim" class="min-w-0 flex-1 truncate">
        {waitingLabel(props.item)}
      </Text>
      <IdleNote inbox={props.inbox} item={props.item} />
    </Row>
  );
}

function IdleNote(props: { inbox: InboxState; item: InboxItem }): JSX.Element {
  return (
    <Show when={idleReason(props.inbox, props.item)}>
      {(reason) => (
        <Text variant="meta" tone="dim" class="shrink-0">
          {reason()}
        </Text>
      )}
    </Show>
  );
}

function OperatorRow(props: {
  inbox: InboxState;
  item: InboxItem;
  actions: InboxActions;
}): JSX.Element {
  // Shared with the thread's bubble and the composer's recall so all three surfaces agree
  // on when the inbox is told to stop delivering the message.
  const edit = createQueuedEdit({
    messageId: () => props.item.id,
    queued: () => true,
    initial: () => props.item.text,
    onHold: (held) => props.actions.onHold?.(props.item.id, held),
    onSave: (text) => props.actions.onEdit?.(props.item.id, text),
  });

  return (
    <Stack gap={1} class="border-t border-line px-3 py-2">
      <Show
        when={edit.editing()}
        fallback={
          <Row justify="between" align="start" gap={2}>
            <Stack gap={1} class="min-w-0 flex-1">
              <Text variant="body" tone="dim">
                {props.item.text}
              </Text>
              <Show when={props.item.turn}>
                <Text variant="meta" tone="dim">
                  After this turn
                </Text>
              </Show>
            </Stack>
            <Row gap={1} align="center">
              <IdleNote inbox={props.inbox} item={props.item} />
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
