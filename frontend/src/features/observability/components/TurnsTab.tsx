import { createMemo, For, Show, type JSX } from "solid-js";
import { duration, longTimestamp, usd } from "~/lib/format";
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
    // Only traced turns are listed, so each is labelled by time, as the transcript
    // stamps it.
    const rows = props.turns.map((t) => ({
      runId: t.runId,
      label: longTimestamp(t.startedAt),
      meta: [duration(t.durationMs), t.costUsd !== null ? usd(t.costUsd) : null]
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
  // A saved message whose time matches no traced turn (older than tracing, or pruned)
  // picks nothing rather than another turn's trace.
  const current = (): string | null => {
    const { runId, at } = props.target;
    if (runId) return runId;
    if (at) return turnAt(props.turns, at)?.runId ?? null;
    return entries()[0]?.runId ?? null;
  };

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
          <Show
            when={current()}
            keyed
            fallback={
              <Text variant="body" tone="dim">
                That turn has no saved trace. Traces are kept for 30 days.
              </Text>
            }
          >
            {(runId) => (
              <TraceWaterfall runId={runId} live={runId === props.liveRunId} />
            )}
          </Show>
        </div>
      </div>
    </Show>
  );
}
