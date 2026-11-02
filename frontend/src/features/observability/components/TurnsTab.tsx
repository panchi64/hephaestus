import { createMemo, For, Show, type JSX } from "solid-js";
import { duration, relativeTime, usd } from "~/lib/format";
import { cx, Text } from "~/ui";
import type { TurnTarget } from "../dialogState";
import { turnAt, type TurnSummary } from "../model";
import { TraceWaterfall } from "./TraceWaterfall";

interface TurnEntry {
  runId: string;
  label: string;
  meta: string;
  live: boolean;
}

/** Every turn of the thread down the left, newest first; the picked one's waterfall on
 *  the right. A turn still running has no rollup yet, so it is listed from its run id. */
export function TurnsTab(props: {
  turns: TurnSummary[];
  liveRunId: string | null;
  target: TurnTarget;
  onSelect: (runId: string) => void;
}): JSX.Element {
  const entries = createMemo((): TurnEntry[] => {
    const rows = props.turns.map((t, i) => ({
      runId: t.runId,
      label: `Turn ${i + 1}`,
      meta: [
        relativeTime(t.startedAt),
        duration(t.durationMs),
        t.costUsd !== null ? usd(t.costUsd) : null,
      ]
        .filter(Boolean)
        .join(" · "),
      live: t.runId === props.liveRunId,
    }));
    const live = props.liveRunId;
    if (live && !rows.some((r) => r.runId === live)) {
      rows.push({
        runId: live,
        label: "This turn",
        meta: "running",
        live: true,
      });
    }
    return rows.reverse();
  });
  const current = (): string | null =>
    props.target.runId ??
    (props.target.at ? turnAt(props.turns, props.target.at)?.runId : null) ??
    entries()[0]?.runId ??
    null;

  return (
    <Show
      when={entries().length > 0}
      fallback={
        <Text variant="body" tone="dim">
          No traced turns yet. Traces are kept for 30 days.
        </Text>
      }
    >
      <div class="flex min-h-0 gap-4">
        <ul class="flex w-48 shrink-0 flex-col" aria-label="Turns">
          <For each={entries()}>
            {(entry) => (
              <li>
                <button
                  type="button"
                  aria-current={entry.runId === current() ? "true" : undefined}
                  onClick={() => props.onSelect(entry.runId)}
                  class={cx(
                    "flex w-full flex-col items-start gap-0.5 border-l-2 px-2 py-1.5 text-left hover:bg-line/40",
                    entry.runId === current()
                      ? "border-bright bg-line/60"
                      : "border-transparent",
                  )}
                >
                  <Text variant="label" tone={entry.live ? "info" : "default"}>
                    {entry.label}
                  </Text>
                  <Text variant="micro" tone="dim" class="tabular-nums">
                    {entry.meta}
                  </Text>
                </button>
              </li>
            )}
          </For>
        </ul>
        <div class="min-w-0 flex-1">
          <Show when={current()} keyed>
            {(runId) => <TraceWaterfall runId={runId} />}
          </Show>
        </div>
      </div>
    </Show>
  );
}
