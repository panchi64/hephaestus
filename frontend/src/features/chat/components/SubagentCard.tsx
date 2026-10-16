import { Show, type JSX } from "solid-js";
import { Button, Text, cx } from "~/ui";
import { duration, parseInstant } from "~/lib/format";
import { isLive, type Subagent, type SubagentStatus } from "../data";
import { SubagentDesignation } from "./SubagentDesignation";

/** The 8px state square. Each state is a different fill, and the two that are not
 *  "finished" say so a second way — running blinks (hard-stepped, the machine
 *  register), blocked is also the row's amber edge and its WAITING tag — so no state
 *  is told apart by hue alone.
 *
 *  `blocked` is the only one that is a *request*: the sub-agent has stopped and is
 *  waiting on the operator, who does not otherwise know it, so the action tier's amber
 *  is spent there and nowhere else. `cancelled` is not an error and is not a success:
 *  the whole row dims — nothing went wrong, and nothing came of it either. */
const GLYPH: Record<SubagentStatus, string> = {
  running: "bg-nominal heph-caret",
  blocked: "bg-warn",
  done: "bg-text",
  failed: "bg-alert",
  cancelled: "bg-dim",
};

/** The status in words, for the row's accessible name — the glyph is decoration to a
 *  screen reader. */
const LABEL: Record<SubagentStatus, string> = {
  running: "working",
  blocked: "waiting for you",
  done: "done",
  failed: "failed",
  cancelled: "stopped",
};

/** How full its context window is, 0–100, or null when nothing has measured it yet.
 *
 *  Null rather than zero for the unmeasured case: an empty bar and a bar nobody has
 *  filled in draw identically, and only one of them means the sub-agent has done
 *  nothing. A sub-agent that has not made a model request yet simply has no gauge. */
function fullness(subagent: Subagent): number | null {
  const { contextUsed: used, contextWindow: window } = subagent;
  if (used === null || window === null || window <= 0) return null;
  return Math.min(100, Math.round((used / window) * 100));
}

/** The text tone until it is worth looking at, then amber, then red — the composer's
 *  own gauge reads the operator's thresholds from the backend, and a sub-agent's row is
 *  a glance rather than a setting, so it takes fixed ones (the tick marks the first). */
function barFill(value: number): string {
  if (value >= 90) return "bg-alert";
  return value >= 75 ? "bg-warn" : "bg-text";
}

/** How long it ran, in the coarsest unit that is still true — `duration()`'s contract,
 *  so it is `duration()`. Only once it has ended: a running figure would be a client
 *  stopwatch, and the backend's timestamps are what this row is allowed to report. */
function took(subagent: Subagent): string | null {
  if (subagent.endedAt === null) return null;
  const ms = parseInstant(subagent.endedAt) - parseInstant(subagent.startedAt);
  if (!Number.isFinite(ms) || ms < 0) return null;
  return duration(ms);
}

/**
 * One sub-agent, as a row of the Agents roster.
 *
 * Three things, and they are the three the operator asked for: **who** it is, **how
 * much room it has left**, and **what it is doing right now**. Everything else about it
 * — the whole transcript — is one click away and deliberately not here, because a row
 * that tried to show the work would be a worse transcript and a much worse row.
 *
 * **A ruled row, not a card** (design §7, §10.14): the roster is one frame of flush
 * rows, read by scanning a column of handles, and a stack of rounded cards inside the
 * pane's own frame was a container inside a container. First line: the state square,
 * the handle bright, the task dim, and a 48px context bar with its figure. Second line:
 * the roster kind and how long it ran in mono, then its latest line.
 *
 * The context bar is here for the same reason the composer carries one: a sub-agent
 * nobody is watching can fill its window and stop, and a bar closing on its tick is
 * something you notice where a number in a row of numbers is something you read.
 */
export function SubagentCard(props: {
  subagent: Subagent;
  /** Open this sub-agent's own view. The row is a way in, not a thing that unfolds —
   *  see `SubagentsSurface` for why the transcript stopped living underneath it. */
  onOpen: () => void;
  /** How many levels it sits below the top of the roster. */
  level?: number;
  /** Cancel its run. */
  onStop: () => void;
  /** Open it as a thread of its own, or go to the thread it already is. */
  onOpenThread: () => void;
}): JSX.Element {
  const s = () => props.subagent;
  // While it runs this is its latest answer rather than a report — a current best. Once
  // it has finished it is what it actually handed back, and a failure replaces it with
  // why, because that is the thing the operator opened the panel for.
  const line = (): string => {
    if (s().status === "failed") return s().error ?? "failed";
    return s().summary ?? (isLive(s()) ? "working…" : "reported nothing");
  };
  const meta = (): string =>
    [s().name, took(s()) ?? (s().status === "cancelled" ? "stopped" : null)]
      .filter(Boolean)
      .join(" · ");

  return (
    <div
      class={cx(
        "flex flex-col",
        (props.level ?? 0) > 0 && "border-l border-line-strong",
      )}
      style={{ "margin-left": `${(props.level ?? 0) * 16}px` }}
    >
      <button
        type="button"
        onClick={() => props.onOpen()}
        aria-label={`${s().handle}, ${LABEL[s().status]}: ${s().task}`}
        class={cx(
          "relative flex w-full items-start gap-3 px-3 py-2 text-left hover:bg-raised",
          s().status === "cancelled" && "opacity-60",
          // Waiting on the operator: the action tier's echo on a row — an amber inset
          // edge and a faint amber wash (design §10.18).
          s().status === "blocked" && "bg-warn/5",
        )}
      >
        <Show when={s().status === "blocked"}>
          <span
            aria-hidden="true"
            class="absolute inset-y-0 left-0 w-0.5 bg-warn"
          />
        </Show>
        <span
          aria-hidden="true"
          class={cx("mt-1 size-2 shrink-0", GLYPH[s().status])}
        />
        <span class="flex min-w-0 flex-1 flex-col gap-1">
          <span class="flex min-w-0 items-baseline gap-3">
            <SubagentDesignation subagent={s()} />
            {/* The handle, not the roster name: the launching agent named this one for
              the job it is doing, and `explorer` three times over identifies nothing.
              Capped and truncated — a handle is whatever the launching model typed. */}
            <Text
              variant="label"
              tone="bright"
              class="max-w-2/5 shrink-0 truncate"
            >
              {s().handle}
            </Text>
            <Text variant="label" tone="dim" class="min-w-0 flex-1 truncate">
              {s().task}
            </Text>
            <Show
              when={s().status !== "blocked"}
              fallback={
                <Text
                  variant="meta"
                  tone="inverse"
                  class="shrink-0 self-center bg-warn px-1"
                >
                  Waiting
                </Text>
              }
            >
              {/* `!== null`, not the value itself: a sub-agent that has barely started
                rounds to 0, and 0 is falsy — a truthiness test would hide the bar for
                exactly the rows that have just appeared and are the ones watched. */}
              <Show
                when={fullness(s()) !== null}
                fallback={
                  <Text variant="micro" tone="dim" class="shrink-0">
                    —
                  </Text>
                }
              >
                <span class="flex shrink-0 items-center gap-2 self-center">
                  <span
                    role="meter"
                    aria-valuenow={fullness(s()) ?? 0}
                    aria-valuemin={0}
                    aria-valuemax={100}
                    aria-label="Context used"
                    class="relative h-1 w-12 bg-line"
                  >
                    <span
                      class={cx(
                        "absolute inset-y-0 left-0",
                        barFill(fullness(s()) ?? 0),
                      )}
                      style={{ width: `${fullness(s()) ?? 0}%` }}
                    />
                    {/* The tick at 75%: where the bar turns amber. */}
                    <span
                      aria-hidden="true"
                      class="absolute -inset-y-0.5 left-3/4 w-px bg-line-strong"
                    />
                  </span>
                  <Text
                    variant="micro"
                    tone="dim"
                    class="w-8 text-right tabular-nums"
                  >
                    {fullness(s())}%
                  </Text>
                </span>
              </Show>
            </Show>
          </span>
          <span class="flex min-w-0 items-baseline gap-2">
            <Text variant="micro" tone="dim" class="shrink-0">
              {meta()}
            </Text>
            <Text
              variant="body"
              tone={s().status === "failed" ? "alert" : "default"}
              class="min-w-0 flex-1 truncate"
            >
              {line()}
            </Text>
          </span>
        </span>
      </button>
      <div class="flex items-center gap-2 px-3 pb-2 pl-8">
        <Show when={isLive(s())}>
          <Button variant="ghost" size="sm" onClick={() => props.onStop()}>
            Stop
          </Button>
        </Show>
        <Button variant="ghost" size="sm" onClick={() => props.onOpenThread()}>
          {s().opened ? "Open thread" : "Open as thread"}
        </Button>
      </div>
    </div>
  );
}
