import { For, Show, createMemo, type JSX } from "solid-js";
import type { ContextMenuApi } from "~/ui";
import { isRetitling } from "../conversationActions";
import { isPinned, titleReveals } from "../data";
import type { ChatSummary } from "../model";
import { SessionRow } from "./SessionRow";
import { ThreadLog, createThreadFold } from "./ThreadLog";

export interface SessionRowsProps {
  sessions: ChatSummary[];
  currentId: string | null;
  menu: ContextMenuApi;
  onSelect: (id: string) => void;
  unread?: (id: string) => boolean;
}

/** The rows of one rail section — a component rather than a shared JSX value, so each
 *  section renders its own nodes instead of passing one set of DOM back and forth
 *  across a `Show`. */
export function SessionRows(props: SessionRowsProps): JSX.Element {
  // An agent thread the operator opened sits in its parent's thread log rather than
  // in the list, and only a parent that is itself listed here can hold it.
  const listed = createMemo(() => new Set(props.sessions.map((s) => s.id)));
  const childrenOf = (id: string) =>
    props.sessions.filter((s) => s.parentId === id && s.id !== id);
  const top = createMemo(() =>
    props.sessions.filter((s) => !s.parentId || !listed().has(s.parentId)),
  );
  /* Iterated by id: every read of the list mints new summaries, and a
     reference-keyed list would remount each row on every refetch, cutting off a
     thread's arrival halfway through. */
  const topIds = createMemo(() => top().map((s) => s.id));
  const byId = createMemo(
    () => new Map(props.sessions.map((s) => [s.id, s] as const)),
  );
  return (
    <For each={topIds()}>
      {(id) => (
        <Show when={byId().get(id)}>
          {(s) => {
            const threads = () => childrenOf(id);
            const fold = createThreadFold(
              threads,
              childrenOf,
              () => props.currentId,
            );
            return (
              <>
                <SessionRow
                  title={s().title}
                  selected={id === props.currentId}
                  pinned={isPinned(id)}
                  unread={props.unread?.(id)}
                  reveal={titleReveals[id]}
                  retitling={isRetitling(id)}
                  activity={s().activity}
                  awaitingKind={s().awaitingKind}
                  lastOutcome={s().lastOutcome}
                  updatedAt={s().updatedAt}
                  onOpen={() => props.onSelect(id)}
                  // The thread's id is the menu's key, which is what lets one panel serve the
                  // whole list: it tells the rail which thread to build items for, and tells
                  // the row whether the open menu is its own.
                  onContextMenu={(e) => props.menu.openAt(e, id)}
                  menuTrigger={props.menu.triggerProps(id)}
                  menuOpen={props.menu.isOpen(id)}
                  fold={
                    threads().length > 0
                      ? {
                          open: fold.open(),
                          onToggle: fold.toggle,
                          label: `Agent threads of ${s().title}`,
                        }
                      : undefined
                  }
                />
                <Show when={threads().length > 0}>
                  <ThreadLog
                    threads={threads()}
                    open={fold.open()}
                    childrenOf={childrenOf}
                    currentId={props.currentId}
                    onSelect={props.onSelect}
                  />
                </Show>
              </>
            );
          }}
        </Show>
      )}
    </For>
  );
}
