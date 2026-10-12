import { For, Show, createSignal, type JSX } from "solid-js";
import { Collapse, Icon, Text, cx } from "~/ui";
import type { ChatSummary } from "../model";
import { Branch } from "./Branch";

/** Whether a row's agent threads are showing: shut until the operator opens them, or
 *  while the thread they are reading is among them. */
export function createThreadFold(
  threads: () => ChatSummary[],
  childrenOf: (id: string) => ChatSummary[],
  currentId: () => string | null,
) {
  const [toggled, setToggled] = createSignal<boolean | null>(null);
  const holdsCurrent = (): boolean => {
    const walk = (list: ChatSummary[]): boolean =>
      list.some((t) => t.id === currentId() || walk(childrenOf(t.id)));
    return walk(threads());
  };
  const open = () => toggled() ?? holdsCurrent();
  return { open, toggle: () => setToggled(!open()) };
}

/** The agent threads opened under a thread, hung off a trunk the way a work log's
 *  steps are. The trunk drops from the row's state square; each thread's socket lines
 *  up with the titles above it. */
export function ThreadLog(props: {
  threads: ChatSummary[];
  open: boolean;
  childrenOf: (id: string) => ChatSummary[];
  currentId: string | null;
  onSelect: (id: string) => void;
  nested?: boolean;
}): JSX.Element {
  const edgeOf = (index: number) => ({
    first: index === 0,
    last: index === props.threads.length - 1,
  });
  return (
    <Collapse open={props.open}>
      <div class={props.nested ? undefined : "pl-0.5"}>
        <For each={props.threads}>
          {(thread, index) => (
            <Branch
              edge={edgeOf(index())}
              variant={props.nested ? "nested" : "log"}
            >
              <ThreadBranch
                thread={thread}
                childrenOf={props.childrenOf}
                currentId={props.currentId}
                onSelect={props.onSelect}
              />
            </Branch>
          )}
        </For>
      </div>
    </Collapse>
  );
}

function ThreadBranch(props: {
  thread: ChatSummary;
  childrenOf: (id: string) => ChatSummary[];
  currentId: string | null;
  onSelect: (id: string) => void;
}): JSX.Element {
  const children = () => props.childrenOf(props.thread.id);
  const fold = createThreadFold(
    children,
    props.childrenOf,
    () => props.currentId,
  );
  const selected = () => props.thread.id === props.currentId;
  const tone = () =>
    props.thread.activity === "awaiting_input"
      ? "text-warn"
      : props.thread.activity
        ? "text-info"
        : "text-dim";
  return (
    <>
      <div
        class={cx(
          "flex w-full min-w-0 items-center hover:bg-raised",
          selected() && "bg-raised",
        )}
      >
        <button
          type="button"
          onClick={() => props.onSelect(props.thread.id)}
          aria-current={selected() ? "page" : undefined}
          title={props.thread.title}
          class="flex min-w-0 flex-1 items-center gap-2 py-1 pr-2 text-left"
        >
          <Icon
            name="users"
            size={12}
            class={cx("box-content shrink-0 border border-line p-px", tone())}
          />
          <Text
            variant="micro"
            tone={selected() ? "bright" : "default"}
            class="min-w-0 truncate"
          >
            {props.thread.title}
          </Text>
        </button>
        <Show when={children().length > 0}>
          <button
            type="button"
            aria-expanded={fold.open()}
            aria-label="Agent threads"
            onClick={() => fold.toggle()}
            class="shrink-0 px-1.5 text-dim hover:text-bright"
          >
            <Icon
              name={fold.open() ? "chevron-down" : "chevron-right"}
              size={12}
            />
          </button>
        </Show>
      </div>
      <Show when={children().length > 0}>
        <ThreadLog
          threads={children()}
          open={fold.open()}
          childrenOf={props.childrenOf}
          currentId={props.currentId}
          onSelect={props.onSelect}
          nested
        />
      </Show>
    </>
  );
}
