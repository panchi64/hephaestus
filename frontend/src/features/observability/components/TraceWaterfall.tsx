import {
  createMemo,
  createResource,
  createSignal,
  For,
  Show,
  type JSX,
} from "solid-js";
import { compactCount, duration, usd } from "~/lib/format";
import { settled } from "~/lib/resource";
import { cx, Text } from "~/ui";
import { fetchRunTrace } from "../data";
import { liveSpans } from "../live";
import { agentLabel, type TraceSpan } from "../model";
import { layoutWaterfall, mergeSpans } from "../waterfall";

/** Luminance by kind; hue only for a failed step (§5). */
const FILL: Record<TraceSpan["kind"], string> = {
  run: "bg-line",
  model: "bg-bright",
  tool: "bg-dim",
};

function rowLabel(span: TraceSpan): string {
  if (span.kind === "tool") return span.toolName ?? "tool";
  if (span.kind === "model") return span.model ?? "model";
  return agentLabel(span.agentName);
}

/** One turn as a waterfall: every agent run, model request and tool call, nested and
 *  placed in time, with what each cost. Live spans fill in while the turn runs. */
export function TraceWaterfall(props: {
  runId: string;
  live: boolean;
}): JSX.Element {
  // Read again when the run stops: its own agent span ends after its stream closes, so
  // it only ever arrives saved.
  const [saved] = createResource(
    () => ({ runId: props.runId, live: props.live }),
    ({ runId }) => fetchRunTrace(runId),
  );
  const layout = createMemo(() =>
    layoutWaterfall(mergeSpans(settled(saved) ?? [], liveSpans(props.runId))),
  );
  const [selected, setSelected] = createSignal<string | null>(null);
  const picked = createMemo(
    () => layout().rows.find((r) => r.span.spanId === selected())?.span,
  );
  const totals = createMemo(() => {
    const models = layout().rows.filter((r) => r.span.kind === "model");
    const priced = models.filter((r) => r.span.costUsd !== null);
    return {
      models: models.length,
      tools: layout().rows.filter((r) => r.span.kind === "tool").length,
      cost: priced.length
        ? priced.reduce((s, r) => s + r.span.costUsd!, 0)
        : null,
    };
  });

  return (
    <div class="flex min-w-0 flex-col gap-3">
      <Show
        when={layout().rows.length > 0}
        fallback={
          <Text variant="body" tone="dim">
            {saved.error
              ? "Couldn't load this turn's trace."
              : saved.loading
                ? "Loading the trace…"
                : "No traced calls for this turn."}
          </Text>
        }
      >
        <div class="flex flex-wrap items-baseline gap-x-4 gap-y-1">
          <Text variant="readout" tone="bright" class="tabular-nums">
            {duration(layout().totalMs)}
          </Text>
          <Text variant="micro" tone="dim" class="tabular-nums">
            {totals().models} model · {totals().tools} tool
            <Show when={totals().cost !== null}> · {usd(totals().cost!)}</Show>
          </Text>
        </div>

        <div role="list" aria-label="Calls in this turn" class="flex flex-col">
          <For each={layout().rows}>
            {(row) => (
              <button
                type="button"
                role="listitem"
                onClick={() =>
                  setSelected((id) =>
                    id === row.span.spanId ? null : row.span.spanId,
                  )
                }
                class={cx(
                  "grid grid-cols-[minmax(0,14rem)_minmax(0,1fr)_4rem_5rem] items-center gap-3 px-1 py-1 text-left hover:bg-line/40",
                  selected() === row.span.spanId && "bg-line/60",
                )}
              >
                <span
                  class="flex min-w-0 items-center gap-1.5"
                  style={{ "padding-left": `${row.depth * 12}px` }}
                >
                  <Text
                    variant="micro"
                    tone="dim"
                    class="w-8 shrink-0 uppercase"
                  >
                    {row.span.kind === "run" ? "agent" : row.span.kind}
                  </Text>
                  <Text
                    variant="micro"
                    tone={row.span.errorType ? "alert" : "default"}
                    class="min-w-0 truncate"
                  >
                    {rowLabel(row.span)}
                  </Text>
                </span>
                <span class="relative h-2.5 w-full bg-line/30">
                  <span
                    class={cx(
                      "absolute inset-y-0",
                      row.span.errorType ? "bg-alert" : FILL[row.span.kind],
                    )}
                    style={{
                      left: `${row.offset}%`,
                      width: `max(${row.width}%, 2px)`,
                    }}
                  />
                </span>
                <Text
                  variant="micro"
                  tone="dim"
                  class="text-right tabular-nums"
                >
                  {duration(row.span.durationMs)}
                </Text>
                <Text
                  variant="micro"
                  tone="default"
                  class="text-right tabular-nums"
                >
                  {row.span.costUsd !== null ? usd(row.span.costUsd) : ""}
                </Text>
              </button>
            )}
          </For>
        </div>

        <Show when={picked()}>{(span) => <SpanDetail span={span()} />}</Show>
      </Show>
    </div>
  );
}

function SpanDetail(props: { span: TraceSpan }): JSX.Element {
  const fields = (): [string, string][] => {
    const s = props.span;
    const out: [string, string][] = [["Agent", agentLabel(s.agentName)]];
    if (s.model) out.push(["Model", s.model]);
    if (s.toolName) out.push(["Tool", s.toolName]);
    out.push(["Duration", duration(s.durationMs)]);
    if (s.ttftMs !== null) out.push(["First token", duration(s.ttftMs)]);
    if (s.inputTokens !== null)
      out.push(["Input", compactCount(s.inputTokens, true)]);
    if (s.outputTokens !== null)
      out.push(["Output", compactCount(s.outputTokens, true)]);
    if (s.cacheReadTokens !== null)
      out.push(["Cache read", compactCount(s.cacheReadTokens, true)]);
    if (s.cacheWriteTokens !== null)
      out.push(["Cache write", compactCount(s.cacheWriteTokens, true)]);
    if (s.costUsd !== null) out.push(["Cost", usd(s.costUsd)]);
    if (s.errorType) out.push(["Error", s.errorType]);
    return out;
  };
  return (
    <dl class="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 border border-line p-3">
      <For each={fields()}>
        {([label, value]) => (
          <>
            <dt>
              <Text variant="meta" tone="dim">
                {label}
              </Text>
            </dt>
            <dd class="min-w-0 truncate">
              <Text
                variant="micro"
                tone={label === "Error" ? "alert" : "default"}
                class="tabular-nums"
              >
                {value}
              </Text>
            </dd>
          </>
        )}
      </For>
    </dl>
  );
}
