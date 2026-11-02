import { createMemo, Show, type JSX } from "solid-js";
import { compactCount, duration, plural, usd } from "~/lib/format";
import { InfoHint, ProgressBar, Text } from "~/ui";
import type { ConversationStats } from "~/features/chat/model";
import { ConversationCompactionToggle } from "~/features/chat/components/ConversationCompactionToggle";
import { ConversationGrants } from "~/features/chat/components/ConversationGrants";
import { LastRequestPanel } from "~/features/chat/components/LastRequestPanel";
import { StackedBar } from "~/features/chat/components/StackedBar";
import type { TurnSummary } from "../model";
import { CostColumns } from "./CostColumns";
import { Figure } from "./Figure";

/** The conversation at a glance: what it cost, how big it is, where the time went, and
 *  which turns were expensive.
 *
 *  Every figure is the backend's. Unmeasured values stay absent rather than zero: a
 *  `0%` cache hit would read as a broken cache rather than an unreported one. */
export function OverviewTab(props: {
  conversationId: () => string | null;
  stats: ConversationStats | null;
  turns: TurnSummary[];
  grantsRevalidate: () => unknown;
  onOpenTurn: (runId: string) => void;
}): JSX.Element {
  const backgroundUsd = createMemo(() => {
    const extra = props.turns.reduce(
      (sum, t) => sum + Math.max((t.costUsd ?? 0) - (t.chatCostUsd ?? 0), 0),
      0,
    );
    return extra > 0 ? extra : null;
  });
  const timeTotal = (s: ConversationStats): number | null =>
    s.llmMs === null && s.toolMs === null
      ? null
      : (s.llmMs ?? 0) + (s.toolMs ?? 0);

  return (
    <div class="flex flex-col gap-5">
      <Show
        when={props.stats && props.stats.steps > 0 ? props.stats : null}
        fallback={
          <Text variant="body" tone="dim">
            Nothing measured yet. Figures appear after the first turn.
          </Text>
        }
      >
        {(s) => (
          <>
            <div class="grid grid-cols-2 gap-2 md:grid-cols-4">
              <Figure
                label="COST"
                value={s().costUsd !== null ? usd(s().costUsd!) : "—"}
                sub={
                  <Show when={s().savedUsd !== null}>
                    saved {usd(s().savedUsd!)} by caching
                  </Show>
                }
                hint="This thread's chat requests at list price. Only shown for endpoints with a published price; a local server has none."
              />
              <Figure
                label="TOKENS"
                value={
                  s().inputTokens !== null
                    ? compactCount(s().inputTokens!)
                    : "—"
                }
                sub={
                  <Show when={s().outputTokens !== null}>
                    in · {compactCount(s().outputTokens!)} out
                  </Show>
                }
                hint="Input counts every turn's replayed history, so it grows far faster than output."
              />
              <Figure
                label="TIME"
                value={
                  timeTotal(s()) !== null ? duration(timeTotal(s())!) : "—"
                }
                sub={
                  <Show when={s().ttftAvgMs !== null}>
                    first token {duration(s().ttftAvgMs!)} avg
                  </Show>
                }
                hint="Wall-clock spent waiting on the model plus running its tools, measured here so it means the same on every endpoint."
              />
              <Figure
                label="WORK"
                value={plural(s().turns, "turn")}
                sub={
                  <>
                    {plural(s().steps, "step")} ·{" "}
                    {plural(s().toolCalls, "tool call")}
                  </>
                }
                hint="Exchanges you've had, the model round-trips they took, and the tools those called. Many steps per turn means a lot of tool work per question."
              />
            </div>

            <Show when={s().llmMs !== null || s().toolMs !== null}>
              <section class="flex flex-col gap-2">
                <div class="flex items-center justify-between">
                  <div class="flex items-center gap-1.5">
                    <Text variant="meta" tone="dim">
                      WHERE THE TIME WENT
                    </Text>
                    <InfoHint
                      label="A slow thread is either the model or the tools, and which one decides what to do about it."
                      size={11}
                    />
                  </div>
                  <Show when={s().tokensPerSecond !== null}>
                    <Text variant="micro" tone="dim" class="tabular-nums">
                      {s().tokensPerSecond!.toFixed(1)} tok/s
                    </Text>
                  </Show>
                </div>
                <StackedBar
                  height="h-2"
                  segments={[
                    {
                      key: "llm",
                      share: ((s().llmMs ?? 0) / (timeTotal(s()) || 1)) * 100,
                      fill: "bg-bright",
                    },
                    {
                      key: "tools",
                      share: ((s().toolMs ?? 0) / (timeTotal(s()) || 1)) * 100,
                      fill: "bg-dim",
                    },
                  ]}
                />
                <div class="flex gap-4">
                  <Legend fill="bg-bright" label="Model" value={s().llmMs} />
                  <Legend fill="bg-dim" label="Tools" value={s().toolMs} />
                </div>
              </section>
            </Show>

            <Show when={s().cacheHitRatio !== null}>
              <ProgressBar
                label="Prompt cache hit"
                value={s().cacheHitRatio! * 100}
                showValue
                tone="info"
              />
            </Show>

            <Show when={props.turns.some((t) => t.costUsd !== null)}>
              <section class="flex flex-col gap-2">
                <div class="flex items-center justify-between">
                  <Text variant="meta" tone="dim">
                    COST PER TURN
                  </Text>
                  <Show when={backgroundUsd()}>
                    {(bg) => (
                      <Text variant="micro" tone="dim" class="tabular-nums">
                        incl. {usd(bg())} background (titles, page distills)
                      </Text>
                    )}
                  </Show>
                </div>
                <CostColumns
                  aria-label="Cost per turn; pick one to open its trace"
                  columns={props.turns.map((t, i) => ({
                    key: t.runId,
                    title: `Turn ${i + 1}: ${usd(t.costUsd ?? 0)}, ${duration(t.durationMs)}, ${plural(t.modelCalls, "model call")}`,
                    totalUsd: t.costUsd ?? 0,
                    primaryUsd: t.chatCostUsd ?? 0,
                  }))}
                  onSelect={props.onOpenTurn}
                />
              </section>
            </Show>

            <Show when={s().lastRequest}>
              {(request) => <LastRequestPanel request={request()} />}
            </Show>
          </>
        )}
      </Show>

      <Show when={props.conversationId() !== null}>
        <section class="flex flex-col gap-1">
          <Text variant="meta" tone="dim">
            THIS CONVERSATION
          </Text>
          <ConversationGrants
            conversationId={props.conversationId}
            revalidate={props.grantsRevalidate}
          />
          <ConversationCompactionToggle conversationId={props.conversationId} />
        </section>
      </Show>
    </div>
  );
}

function Legend(props: {
  fill: string;
  label: string;
  value: number | null;
}): JSX.Element {
  return (
    <Show when={props.value !== null}>
      <span class="flex items-center gap-1.5">
        <span aria-hidden="true" class={`size-2 ${props.fill}`} />
        <Text variant="micro" tone="default" class="tabular-nums">
          {props.label} {duration(props.value!)}
        </Text>
      </span>
    </Show>
  );
}
