import { Show, type JSX } from "solid-js";
import { Button, ListGroupHeader, Row, Text, Tooltip } from "~/ui";
import { SessionRows, type SessionRowsProps } from "./SessionRows";

export interface PrivateSectionProps extends SessionRowsProps {
  unlocked: boolean;
  onUnlock: () => void;
  onLock: () => void;
  /** Stage a new thread filed in the area. */
  onNewThread: () => void;
}

/** The Private area at the foot of the rail. Locked, it is a heading and an Unlock
 *  control, with no titles and no count: the backend withholds the rows, and a count
 *  would still say how much is in there. */
export function PrivateSection(props: PrivateSectionProps): JSX.Element {
  return (
    <div class="pb-1">
      <ListGroupHeader
        variant="plate"
        label="Private"
        count={props.unlocked ? props.sessions.length : undefined}
        aside={
          <Show
            when={props.unlocked}
            fallback={
              <Button
                variant="ghost"
                size="sm"
                leading="lock"
                onClick={() => props.onUnlock()}
              >
                Unlock
              </Button>
            }
          >
            <Row gap={1}>
              <Tooltip label="New private chat" side="right">
                <Button
                  variant="ghost"
                  size="sm"
                  leading="plus"
                  aria-label="New private chat"
                  class="size-7 shrink-0"
                  onClick={() => props.onNewThread()}
                />
              </Tooltip>
              <Tooltip label="Lock private chats" side="right">
                <Button
                  variant="ghost"
                  size="sm"
                  leading="lock"
                  aria-label="Lock private chats"
                  class="size-7 shrink-0"
                  onClick={() => props.onLock()}
                />
              </Tooltip>
            </Row>
          </Show>
        }
      />
      <Show when={props.unlocked}>
        <Show
          when={props.sessions.length > 0}
          fallback={
            <Text variant="caption" tone="dim" class="block px-3 py-1.5">
              No private chats. Start one with +, or move a chat here from its
              menu.
            </Text>
          }
        >
          <SessionRows
            sessions={props.sessions}
            currentId={props.currentId}
            menu={props.menu}
            onSelect={props.onSelect}
            unread={props.unread}
          />
        </Show>
      </Show>
    </div>
  );
}
