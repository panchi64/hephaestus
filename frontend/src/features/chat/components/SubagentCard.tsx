import { Show, type JSX } from "solid-js";
import { Button, MetClock, RailTag, Text, cx } from "~/ui";
import { isLive, type Subagent, type SubagentStatus } from "../data";
import { fullness, gaugeTone, latestLine } from "../subagentRoster";

/** The ledger's tracks: three below the pane's `@2xl`, five at it. The roster's column
 *  header uses the same string so its legends land on the rows' columns. */
export const LEDGER_COLUMNS =
  "grid grid-cols-[72px_minmax(0,1fr)_56px] gap-x-2 @2xl:grid-cols-[136px_minmax(0,1.2fr)_minmax(0,1fr)_56px_56px] @2xl:gap-x-3";

/** No state is told apart by hue alone: running also blinks, blocked also takes the
 *  amber edge and the WAITING tag, cancelled also dims the row. */
const GLYPH: Record<SubagentStatus, string> = {
  running: "bg-nominal heph-caret",
  blocked: "bg-warn",
  done: "bg-text",
  failed: "bg-alert",
  cancelled: "bg-dim",
  stopped: "bg-dim",
};

/** For the row's accessible name; the glyph is hidden from screen readers. */
const LABEL: Record<SubagentStatus, string> = {
  running: "working",
  blocked: "waiting for you",
  done: "done",
  failed: "failed",
  cancelled: "stopped",
  stopped: "stopped at a limit",
};

/** Running: the live edge (§10.9). Blocked: the action tier's echo (§10.18). Both are
 *  inset shadows so marking a row costs no layout. */
const EDGE: Partial<Record<SubagentStatus, string>> = {
  running: "shadow-[inset_1px_0_0_var(--color-info)]",
  blocked: "bg-warn/5 shadow-[inset_2px_0_0_var(--color-warn)]",
  cancelled: "opacity-60",
  stopped: "opacity-60",
};

const GAUGE_FILL = {
  default: "bg-text",
  warn: "bg-warn",
  alert: "bg-alert",
} as const;

/** One sub-agent as a ledger row: who, what it was asked, what it last said, and how
 *  much room and time it has used. The transcript is one click away, in
 *  `SubagentDetail`. */
export function SubagentCard(props: {
  subagent: Subagent;
  /** Open this sub-agent's own view. */
  onOpen: () => void;
  /** How many levels it sits below the top of the roster. */
  level?: number;
  onStop: () => void;
  /** Open it as a thread of its own, or go to the thread it already is. */
  onOpenThread: () => void;
}): JSX.Element {
  const s = () => props.subagent;
  const level = () => props.level ?? 0;
  const full = () => fullness(s());
  const tone = () => gaugeTone(full() ?? 0);
  const blocked = () => s().status === "blocked";
  const failed = () => s().status === "failed";
  const latest = () => latestLine(s());

  const percent = (): JSX.Element => (
    // `!== null`: a sub-agent that has just started rounds to 0, which is falsy.
    <Show
      when={full() !== null}
      fallback={
        <Text variant="micro" tone="dim">
          —
        </Text>
      }
    >
      <Text
        variant="micro"
        tone={tone() === "default" ? "dim" : tone()}
        class="tabular-nums"
      >
        {full()}%
      </Text>
    </Show>
  );

  const clock = (): JSX.Element => (
    <MetClock
      startedAt={s().startedAt}
      endedAt={s().endedAt}
      variant="micro"
      tone="dim"
      prefix={false}
    />
  );

  return (
    <div class="group relative" title={s().handle}>
      <button
        type="button"
        onClick={() => props.onOpen()}
        aria-label={`${s().handle}, ${LABEL[s().status]}: ${s().task}`}
        class={cx(
          LEDGER_COLUMNS,
          "w-full items-start px-2 py-1.5 text-left hover:bg-raised",
          EDGE[s().status],
        )}
      >
        <span
          class="flex min-w-0 items-center gap-2 pt-0.5"
          style={{ "padding-left": `${level() * 12}px` }}
        >
          <Show when={level() > 0}>
            <Text variant="micro" tone="dim" aria-hidden="true">
              └
            </Text>
          </Show>
          <span
            aria-hidden="true"
            class={cx("size-2 shrink-0", GLYPH[s().status])}
          />
          {/* The handle stands in for rows written before callsigns. */}
          <Text variant="code" tone="bright" class="min-w-0 truncate">
            {s().callsign || s().handle}
          </Text>
          <Show when={s().callsign && s().agentClass}>
            <Text
              variant="label"
              tone="dim"
              class="hidden min-w-0 truncate @2xl:inline"
            >
              {s().agentClass}
            </Text>
          </Show>
        </span>

        <span class="flex min-w-0 flex-col gap-0.5">
          <Text
            variant="body"
            tone={isLive(s()) ? "bright" : "default"}
            class="line-clamp-2 break-words @2xl:line-clamp-1"
          >
            {s().task}
          </Text>
          <Text
            variant="micro"
            tone={failed() ? "alert" : "dim"}
            class="truncate @2xl:hidden"
          >
            {latest()}
          </Text>
        </span>

        <Text
          variant="micro"
          tone={failed() ? "alert" : "dim"}
          class="hidden truncate pt-1 @2xl:block"
        >
          {latest()}
        </Text>

        {/* Narrow readout: context over its gauge over the clock. */}
        <span class="flex flex-col items-end gap-0.5 pt-0.5 @2xl:hidden">
          <Show when={!blocked()} fallback={<RailTag>Waiting</RailTag>}>
            {percent()}
            <Show when={full() !== null}>
              <span
                role="meter"
                aria-valuenow={full() ?? 0}
                aria-valuemin={0}
                aria-valuemax={100}
                aria-label="Context used"
                class="relative h-0.5 w-full bg-line"
              >
                <span
                  class={cx("absolute inset-y-0 left-0", GAUGE_FILL[tone()])}
                  style={{ width: `${full() ?? 0}%` }}
                />
              </span>
            </Show>
          </Show>
          {clock()}
        </span>

        <span class="hidden pt-1 @2xl:block">
          <Show when={!blocked()} fallback={<RailTag>Waiting</RailTag>}>
            {percent()}
          </Show>
        </span>
        <span class="hidden pt-1 text-right @2xl:block">{clock()}</span>
      </button>

      {/* Over the readout on hover or focus. A sibling of the row's button, never
          inside it, so no control nests in another. */}
      <div
        class={cx(
          "absolute inset-y-0 right-0 flex w-18 items-center justify-end gap-1 bg-raised pr-2 @2xl:w-33",
          "pointer-events-none opacity-0 group-focus-within:pointer-events-auto group-focus-within:opacity-100 group-hover:pointer-events-auto group-hover:opacity-100",
        )}
      >
        <Show when={isLive(s())}>
          <Button
            variant="ghost"
            size="sm"
            leading="stop"
            aria-label={`Stop ${s().handle}`}
            title="Stop"
            onClick={() => props.onStop()}
          />
        </Show>
        <Button
          variant="ghost"
          size="sm"
          leading="chat"
          aria-label={s().opened ? "Open thread" : "Open as thread"}
          title={s().opened ? "Open thread" : "Open as thread"}
          onClick={() => props.onOpenThread()}
        />
      </div>
    </div>
  );
}
