import { createMemo, Show, type JSX } from "solid-js";
import { compactCount, duration, pct } from "~/lib/format";
import { RevealPopover, StatusCell, Text } from "~/ui";
import type { ConversationStats } from "../model";
import { ConversationCompactionToggle } from "./ConversationCompactionToggle";
import { ConversationGrants } from "./ConversationGrants";
import { StatRow } from "./StatRow";

const tokens = (n: number) => n.toLocaleString("en-US");
const plural = (n: number, one: string, many: string) =>
  `${n} ${n === 1 ? one : many}`;

/** What the thread has cost, and the two settings that belong to it, behind one
 *  `stats` cell in the composer's status bar.
 *
 *  **Why a panel and not the line it replaced.** These figures were a row of
 *  `micro` segments under the input, each with its meaning in a tooltip. Most turns
 *  need none of them, and all of them together were loud enough to bury the thing
 *  they sat next to — every readout individually small enough to dismiss. A trigger
 *  costs one word; the panel spends the room the line never had on saying what each
 *  number means, in a sentence rather than on hover.
 *
 *  **What the numbers count.** Everything is cumulative over the *conversation*, not
 *  the last run — the line used to report a single turn and reset at the start of the
 *  next, which meant the one moment an operator wants to know what a long thread has
 *  spent was the moment it went blank. `turns` are the operator's own exchanges and
 *  `steps` the model round-trips inside them, so the ratio is itself information.
 *
 *  **Everything unmeasured stays absent** — not zeroed. A `null` from the backend means
 *  nobody reported that figure (an endpoint that sends no cache tokens, turns recorded
 *  before the stopwatch existed), and a `0%` cache hit would read as a broken cache
 *  rather than an unreported one.
 *
 *  **The grants and auto-compaction live here too**, under "This conversation": they are
 *  per-thread settings, not glance items, and keeping them on the row would rebuild the
 *  clutter this panel exists to clear.
 *
 *  Presentation only — every value is the backend's, rendered, never derived here. The
 *  averages and rates arrive already computed for exactly that reason. */
export function ConversationStatsPanel(props: {
  conversationId: () => string | null;
  /** What the thread has cost — cumulative, backend-derived. */
  stats: () => ConversationStats | null | undefined;
  /** Ticks when a grant may have changed, so the grants row refetches. */
  grantsRevalidate: () => unknown;
}): JSX.Element {
  // A thread that has run at all has counted at least one step. Guarding on the object
  // alone would render a panel of zeroes for the beat before the first frame.
  const stats = createMemo(() => {
    const s = props.stats();
    return s && s.steps > 0 ? s : undefined;
  });

  // Offered once there is anything behind it: figures to read, or — once the thread has
  // an id — its settings, of which auto-compaction always exists. A fresh unsaved
  // composer has neither, and a trigger opening onto nothing is a control that lies.
  const shown = () => stats() !== undefined || props.conversationId() !== null;

  return (
    <Show when={shown()}>
      <RevealPopover
        align="right"
        panelClass="w-80"
        trigger={({ open, setOpen }) => (
          <StatusCell
            active={open()}
            aria-expanded={open()}
            aria-label="Conversation stats and settings"
            onClick={() => setOpen(!open())}
          >
            Stats
          </StatusCell>
        )}
      >
        {() => (
          <>
            <Show when={stats()}>
              {(s) => (
                <>
                  <Text variant="plate" tone="dim">
                    Totals
                  </Text>

                  <StatRow
                    label="Work"
                    value={
                      <>
                        {plural(s().turns, "turn", "turns")} ·{" "}
                        {plural(s().steps, "step", "steps")}
                        <Show when={s().toolCalls > 0}>
                          {" · "}
                          {plural(s().toolCalls, "tool call", "tool calls")}
                        </Show>
                      </>
                    }
                    hint="Exchanges you've had, and the model round-trips they took. A high step count per turn means the agent is doing a lot of tool work per question."
                  />

                  {/* Where the time went. The two halves are the whole wait split in
                      one place — a slow thread is either the model or the tools, and
                      which one it is decides what to do about it. */}
                  <Show when={s().llmMs !== null || s().toolMs !== null}>
                    <StatRow
                      label="Time"
                      value={
                        <>
                          <Show when={s().llmMs !== null}>
                            LLM {duration(s().llmMs!)}
                          </Show>
                          <Show when={s().llmMs !== null && s().toolMs}>
                            {" · "}
                          </Show>
                          <Show when={s().toolMs}>
                            tools {duration(s().toolMs!)}
                          </Show>
                        </>
                      }
                      hint="Wall-clock spent waiting on the model, and running its tools. Measured here rather than reported by the provider, so it means the same on every endpoint."
                    />
                  </Show>

                  {/* Responsiveness: how long until something appears, and how fast it
                      arrives once it does. Two different complaints about one model. */}
                  <Show
                    when={
                      s().ttftAvgMs !== null || s().tokensPerSecond !== null
                    }
                  >
                    <StatRow
                      label="Speed"
                      value={
                        <>
                          <Show when={s().ttftAvgMs !== null}>
                            TTFT avg {duration(s().ttftAvgMs!)}
                          </Show>
                          <Show
                            when={
                              s().ttftAvgMs !== null &&
                              s().tokensPerSecond !== null
                            }
                          >
                            {" · "}
                          </Show>
                          <Show when={s().tokensPerSecond !== null}>
                            {s().tokensPerSecond!.toFixed(1)} tok/s
                          </Show>
                        </>
                      }
                      hint="Average wait before the model produces anything (reasoning counts), and its generation speed once it starts."
                    />
                  </Show>

                  {/* Absent, not zero: most OpenAI-compatible and local endpoints never
                      report a cache figure, and "0%" would read as a fault. */}
                  <Show when={s().cacheHitRatio !== null}>
                    <StatRow
                      label="Cache hit"
                      value={pct(s().cacheHitRatio! * 100)}
                      hint="Share of prompt tokens the provider served from its own cache. Only shown for providers that report it."
                    />
                  </Show>

                  {/* The value is abbreviated because the magnitude is what's read; the
                      exact figures ride the explanation, where a seven-digit number can
                      be looked at rather than scanned past. */}
                  <Show when={s().inputTokens !== null}>
                    <StatRow
                      label="Tokens"
                      value={
                        <>
                          In {compactCount(s().inputTokens!)}
                          <Show when={s().outputTokens !== null}>
                            {" · out "}
                            {compactCount(s().outputTokens!)}
                          </Show>
                        </>
                      }
                      hint={`${tokens(s().inputTokens!)} in, ${tokens(s().outputTokens ?? 0)} out, across the whole thread. Input counts every turn's replayed history, so it grows far faster than output.`}
                    />
                  </Show>
                </>
              )}
            </Show>

            {/* Both rows resolve their own presence from their own resource, so the
                heading is gated on the one fact that guarantees at least one of them:
                a thread with an id always has an auto-compaction setting. */}
            <Show when={props.conversationId() !== null}>
              <Text variant="plate" tone="dim" class="pt-1">
                This conversation
              </Text>
              <ConversationGrants
                conversationId={props.conversationId}
                revalidate={props.grantsRevalidate}
              />
              <ConversationCompactionToggle
                conversationId={props.conversationId}
              />
            </Show>
          </>
        )}
      </RevealPopover>
    </Show>
  );
}
