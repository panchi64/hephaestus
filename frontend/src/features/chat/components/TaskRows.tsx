import { createMemo, createSignal, For, Show, type JSX } from "solid-js";
import { cx, Frames, Icon, Text } from "~/ui";
import type { TaskItem } from "~/lib/stream";
import { taskLabel } from "../taskItems";

/** Compact window: the readout shows at most this many rows — enough to see
 *  where work is and what comes next without pushing the transcript off screen. */
const WINDOW = 5;

/** Status square (§6.7 list row, §4 semantic color): filled nominal green = done;
 *  empty + bordered = not started, cancelled or blocked. Fill vs border carries
 *  the state in shape, so hue is never the only signal (§9).
 *
 *  `in_progress` is deliberately absent: a running task is the one state here with
 *  *duration*, and a static square said so in colour alone — the same blue a
 *  finished-and-blue square would wear. It gets `Frames` instead, the system's one
 *  "working now" indicator (§8), so motion carries the state that is about time. */
const SQUARE: Record<Exclude<TaskItem["status"], "in_progress">, string> = {
  completed: "bg-nominal",
  pending: "border border-dim",
  cancelled: "border border-line",
  blocked: "border border-alert",
};

const TONE: Record<TaskItem["status"], "dim" | "default" | "bright" | "alert"> =
  {
    completed: "default",
    in_progress: "bright",
    pending: "default",
    cancelled: "dim",
    blocked: "alert",
  };

/** Recency fade (§3 hierarchy = brightness): the row at the frontier — where work
 *  currently is — reads at full brightness, each step away dims. Stepped values,
 *  not a gradient: deterministic readout, no easing (§8). */
const fade = (distance: number) =>
  distance === 0
    ? "opacity-100"
    : distance === 1
      ? "opacity-75"
      : distance === 2
        ? "opacity-50"
        : "opacity-30";

/** The agent's task list for this thread, as rows.
 *
 *  Shows a compact window around the frontier — the running task, else the next pending
 *  one, else the last item of a finished list — so progress is legible at a glance with
 *  no click: green squares for done, blue for running, empty bordered squares for what's
 *  left. Longer lists fold to the window behind an explicit +N MORE control; expanding
 *  lists every row.
 *
 *  The done/total count and the ACTIVE flag are *not* here — they live in the
 *  composer's `Tasks` toggle, which is also what opens these rows. This renders only
 *  what that one-word summary can't say.
 *
 *  Presentation only: the backend owns the list and nothing here can change it. There is
 *  deliberately no edit affordance — the list is the agent's account of its own work, and
 *  an operator edit would silently disagree with what the model reads back.
 */
export function TaskRows(props: {
  items: () => TaskItem[];
  /** List every row, with no window and no +N MORE — for a list that has the room
   *  to scroll the whole of itself. */
  all?: boolean;
}): JSX.Element {
  const [expanded, setExpanded] = createSignal(false);

  const items = () => props.items();

  // Where the work currently is: the running task, else the next pending one,
  // else the last item (a finished list reads from its end).
  const frontier = createMemo(() => {
    const list = items();
    if (list.length === 0) return -1;
    const running = list.findIndex((i) => i.status === "in_progress");
    if (running !== -1) return running;
    const next = list.findIndex((i) => i.status === "pending");
    if (next !== -1) return next;
    return list.length - 1;
  });

  // Rows on screen: the full list when expanded, else a WINDOW-row slice starting two
  // before the frontier (two done behind it, the frontier itself, up to two ahead).
  const visible = createMemo(() => {
    const list = items();
    if (props.all || expanded())
      return list.map((item, index) => ({ item, index }));
    const start = Math.max(0, frontier() - 2);
    return list
      .slice(start, start + WINDOW)
      .map((item, i) => ({ item, index: start + i }));
  });

  const hidden = createMemo(() => items().length - visible().length);

  return (
    <Show when={items().length > 0}>
      <div class="py-1">
        {/* Each row is `items-start`, not `items-center`: a task is whatever the agent
            wrote, and the long ones wrap (see the `Text` inside). Centred, the square
            would float to the middle of a three-line row instead of marking its first
            line, so it takes the first line's own offset. */}
        <ol>
          <For each={visible()}>
            {({ item, index }) => (
              <li
                class={cx(
                  "flex items-start gap-2 px-3 py-1",
                  fade(Math.abs(index - frontier())),
                )}
              >
                {/* The throbber and the square occupy the same 12px column and the
                    same first-line offset, so a task flipping to running (and off
                    it again) swaps a glyph without moving the text beside it. */}
                <Show
                  when={item.status === "in_progress"}
                  fallback={
                    <span
                      class={cx(
                        "mt-1 size-3 shrink-0",
                        SQUARE[item.status as keyof typeof SQUARE],
                      )}
                      aria-hidden
                    />
                  }
                >
                  {/* `size-3` + `leading-3`: literally the square's own box, so the
                      glyph is centred where the square was rather than sitting on the
                      text's baseline. It is a flex item here, which is what makes a
                      height apply to a span at all. */}
                  <Frames class="mt-1 size-3 shrink-0 text-center leading-3 text-info" />
                </Show>
                {/* **Wraps, never truncates.** These rows live in a strip beside the
                    transcript, and a task clipped at the pane's edge is a task the
                    operator cannot read at all — the list is the agent's account of what
                    it is doing, so a row that only shows its first four words is not
                    showing the thing the panel is for. `min-w-0` is what lets the flex
                    child actually break rather than overflowing its parent. */}
                <Text
                  variant="body"
                  tone={TONE[item.status]}
                  class={cx(
                    "min-w-0 flex-1 break-words",
                    item.status === "cancelled" && "line-through",
                  )}
                >
                  {taskLabel(item)}
                </Text>
              </li>
            )}
          </For>
        </ol>
        <Show when={hidden() > 0}>
          <button
            type="button"
            class="flex w-full items-center gap-2 px-3 py-1 text-left"
            aria-expanded={expanded()}
            onClick={() => setExpanded((v) => !v)}
          >
            <Icon
              name={expanded() ? "chevron-up" : "chevron-down"}
              size={12}
              class="text-dim"
            />
            <Text variant="label" tone="dim">
              {expanded() ? "Collapse" : `+${hidden()} MORE`}
            </Text>
          </button>
        </Show>
      </div>
    </Show>
  );
}
