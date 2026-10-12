import { For, Show, createSignal, type JSX } from "solid-js";
import { Collapse, Icon, Text, cx } from "~/ui";
import type { ChatSummary } from "../model";
import { Branch } from "./Branch";
import { ProcessRow, SchematicContext } from "./ProcessRow";

/** The agent threads opened under a thread, drawn as a work log: a header that folds,
 *  and each thread hung off its trunk. Shut until the operator opens it, or while the
 *  thread they are reading is inside it. */
export function ThreadLog(props: {
  threads: ChatSummary[];
  childrenOf: (id: string) => ChatSummary[];
  currentId: string | null;
  onSelect: (id: string) => void;
}): JSX.Element {
  const [toggled, setToggled] = createSignal<boolean | null>(null);
  const holdsCurrent = (): boolean => {
    const walk = (threads: ChatSummary[]): boolean =>
      threads.some(
        (t) => t.id === props.currentId || walk(props.childrenOf(t.id)),
      );
    return walk(props.threads);
  };
  const open = () => toggled() ?? holdsCurrent();
  const edgeOf = (index: number) => ({
    first: index === 0,
    last: index === props.threads.length - 1,
  });

  return (
    <div class="relative">
      <Show when={open()}>
        <span
          aria-hidden="true"
          class="pointer-events-none absolute top-4.5 left-3.5 h-1.5 w-px bg-line"
        />
      </Show>
      <ProcessRow
        open={open()}
        onToggle={() => setToggled(!open())}
        label="Agent threads"
        class="hover:bg-raised"
        trailing={
          <Text variant="micro" tone="dim" class="tabular-nums">
            {props.threads.length}
          </Text>
        }
      />
      <Collapse open={open()}>
        <SchematicContext.Provider value={true}>
          <For each={props.threads}>
            {(thread, index) => (
              <Branch edge={edgeOf(index())}>
                <ThreadBranch
                  thread={thread}
                  selected={thread.id === props.currentId}
                  onSelect={props.onSelect}
                />
                <Show when={props.childrenOf(thread.id).length > 0}>
                  <ThreadLog
                    threads={props.childrenOf(thread.id)}
                    childrenOf={props.childrenOf}
                    currentId={props.currentId}
                    onSelect={props.onSelect}
                  />
                </Show>
              </Branch>
            )}
          </For>
        </SchematicContext.Provider>
      </Collapse>
    </div>
  );
}

function ThreadBranch(props: {
  thread: ChatSummary;
  selected: boolean;
  onSelect: (id: string) => void;
}): JSX.Element {
  const tone = () =>
    props.thread.activity === "awaiting_input"
      ? "text-warn"
      : props.thread.activity
        ? "text-info"
        : "text-dim";
  return (
    <button
      type="button"
      onClick={() => props.onSelect(props.thread.id)}
      aria-current={props.selected ? "page" : undefined}
      title={props.thread.title}
      class={cx(
        "flex w-full min-w-0 items-center gap-2 py-1 pr-2 text-left hover:bg-raised",
        props.selected && "bg-raised",
      )}
    >
      <Icon
        name="users"
        size={12}
        class={cx("box-content shrink-0 border border-line p-px", tone())}
      />
      <Text
        variant="micro"
        tone={props.selected ? "bright" : "default"}
        class="min-w-0 truncate"
      >
        {props.thread.title}
      </Text>
    </button>
  );
}
