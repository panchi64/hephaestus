import { For, Show, createMemo, createSignal, type JSX } from "solid-js";
import { Collapse, Icon, Text, cx } from "~/ui";
import { useSessionArrivals } from "../data";
import type { ChatSummary } from "../model";
import { useArrival } from "./arrival";
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
 *  up with the titles above it. A thread the latest read of the list added traces in,
 *  the same arrival as a work log's new call. */
export function ThreadLog(props: {
  threads: ChatSummary[];
  open: boolean;
  childrenOf: (id: string) => ChatSummary[];
  currentId: string | null;
  onSelect: (id: string) => void;
  nested?: boolean;
}): JSX.Element {
  const arrivals = useSessionArrivals();
  // By id: each read of the list mints new summaries, and a remount would cut an
  // arrival off halfway.
  const ids = createMemo(() => props.threads.map((t) => t.id));
  const byId = createMemo(
    () => new Map(props.threads.map((t) => [t.id, t] as const)),
  );
  const edgeOf = (index: number) => ({
    first: index === 0,
    last: index === props.threads.length - 1,
  });
  return (
    <Collapse open={props.open}>
      <div class={props.nested ? undefined : "pl-0.5"}>
        <For each={ids()}>
          {(id, index) => {
            const trace = arrivals().has(id);
            return (
              <Show when={byId().get(id)}>
                {(thread) => (
                  <Branch
                    edge={edgeOf(index())}
                    variant={props.nested ? "nested" : "log"}
                    trace={trace}
                  >
                    <ThreadBranch
                      thread={thread()}
                      childrenOf={props.childrenOf}
                      currentId={props.currentId}
                      onSelect={props.onSelect}
                    />
                  </Branch>
                )}
              </Show>
            );
          }}
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
  const arrival = useArrival();
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
          arrival.body,
        )}
      >
        <button
          type="button"
          onClick={() => props.onSelect(props.thread.id)}
          aria-current={selected() ? "page" : undefined}
          title={props.thread.title}
          ref={arrival.sweep}
          class="flex min-w-0 flex-1 items-center gap-2 py-1 pr-2 text-left"
        >
          <Icon
            name="users"
            size={12}
            class={cx(
              "box-content shrink-0 border border-line p-px",
              tone(),
              arrival.socket,
            )}
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
