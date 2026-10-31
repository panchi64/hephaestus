import { For, Show, type JSX } from "solid-js";
import { compactCount, sentenceCase, usd } from "~/lib/format";
import { Text } from "~/ui";
import type { LastRequest } from "../model";

/** One reported figure, in the order they are read: what went up, what came back, and
 *  what the provider's cache did with the prompt. */
interface Figure {
  label: string;
  value: number | null;
}

/** The last model request on its own, under the window breakdown it belongs to.
 *
 *  **Why it sits here and not in the stats panel.** Everything in that panel
 *  is cumulative over the thread, which is the right frame for "what has this cost" and
 *  the wrong one for the two questions this answers. Which endpoint served the last
 *  request is invisible in a sum — a fallback chain's second model disappears into the
 *  same total as the first — and a cache that missed on this turn disappears into a
 *  hit ratio averaged over forty. Both are per-request facts, and the popover is where
 *  the operator is already looking at one request's worth of measurement.
 *
 *  **Absent, never zero**, like everything else in this readout: a null is the provider
 *  declining to say, and most OpenAI-compatible and local endpoints decline on caching
 *  entirely. A row of zeroes would read as "your caching is broken" rather than "nobody
 *  said", so a figure that wasn't reported simply isn't drawn — and when none of them
 *  were, the whole block goes with them and only the route remains. */
export function LastRequestPanel(props: { request: LastRequest }): JSX.Element {
  const figures = (): Figure[] =>
    (
      [
        { label: "In", value: props.request.inputTokens },
        { label: "Out", value: props.request.outputTokens },
        { label: "Cache read", value: props.request.cacheReadTokens },
        { label: "Cache write", value: props.request.cacheWriteTokens },
      ] satisfies Figure[]
    ).filter((figure) => figure.value !== null);
  const measured = () => figures().length > 0 || props.request.costUsd !== null;
  return (
    <Show when={props.request.route || measured()}>
      {/* A hairline above rather than a heading: this is a second measurement of the
          same request, not a second section of the panel. */}
      <div class="flex flex-col gap-1 border-t border-line pt-2.5">
        <div class="flex items-baseline justify-between gap-3">
          <Text variant="label" tone="dim">
            Last request
          </Text>
          <Show when={props.request.route}>
            {(route) => (
              /* The model that actually answered — which on a fallback chain is not
                 necessarily the one the thread is bound to, and that difference is the
                 whole reason this is worth a line. */
              <Text variant="micro" tone="default" class="min-w-0 truncate">
                {route()}
              </Text>
            )}
          </Show>
        </div>
        <Show when={measured()}>
          <div class="flex flex-wrap items-baseline gap-x-3 gap-y-0.5">
            <For each={figures()}>
              {(figure) => (
                <Text variant="micro" tone="dim">
                  {figure.label}{" "}
                  <span class="tabular-nums text-text">
                    {compactCount(figure.value!, true)}
                  </span>
                </Text>
              )}
            </For>
            <Show when={props.request.costUsd}>
              {(cost) => (
                <Text variant="micro" tone="dim">
                  Cost <span class="tabular-nums text-text">{usd(cost())}</span>
                </Text>
              )}
            </Show>
          </div>
        </Show>
        {/* Anthropic's own diagnosis of a missed cache: the part of the prompt that changed
            since the previous request, which no token count can say. */}
        <Show when={props.request.cacheMissReason}>
          {(reason) => (
            <Text variant="micro" tone="warn">
              Cache miss: {sentenceCase(reason())}
              <Show when={props.request.cacheMissedTokens}>
                {(missed) => (
                  <>
                    {" · ~"}
                    <span class="tabular-nums">
                      {compactCount(missed(), true)}
                    </span>
                    {" tokens"}
                  </>
                )}
              </Show>
            </Text>
          )}
        </Show>
      </div>
    </Show>
  );
}
