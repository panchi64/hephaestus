import { For, Show, type JSX } from "solid-js";
import { usd } from "~/lib/format";
import { Text } from "~/ui";

export interface CostColumn {
  key: string;
  /** Hover text: what the column is and what it cost. */
  title: string;
  totalUsd: number;
  /** The share drawn bright, on top of a dim remainder — chat versus background. */
  primaryUsd?: number;
}

/** Square columns scaled to the dearest one, so an expensive turn or day stands out at
 *  a glance. Luminance carries the split; hue stays reserved for severity. */
export function CostColumns(props: {
  columns: CostColumn[];
  onSelect?: (key: string) => void;
  "aria-label": string;
}): JSX.Element {
  const max = (): number =>
    Math.max(...props.columns.map((c) => c.totalUsd), 0);
  const share = (value: number): string =>
    `${max() > 0 ? (value / max()) * 100 : 0}%`;

  return (
    <div class="flex flex-col gap-1">
      <div
        role="list"
        aria-label={props["aria-label"]}
        class="flex h-20 items-end gap-px"
      >
        <For each={props.columns}>
          {(column) => (
            <button
              type="button"
              role="listitem"
              title={column.title}
              aria-label={column.title}
              disabled={!props.onSelect}
              onClick={() => props.onSelect?.(column.key)}
              class="flex h-full min-w-1 flex-1 flex-col justify-end hover:opacity-80 disabled:hover:opacity-100"
            >
              <span
                class="flex w-full flex-col"
                style={{ height: share(column.totalUsd) }}
              >
                <span class="w-full flex-1 bg-dim" />
                <Show when={column.primaryUsd !== undefined}>
                  <span
                    class="w-full bg-bright"
                    style={{
                      height: `${column.totalUsd > 0 ? ((column.primaryUsd ?? 0) / column.totalUsd) * 100 : 0}%`,
                    }}
                  />
                </Show>
              </span>
            </button>
          )}
        </For>
      </div>
      <div class="flex justify-between">
        <Text variant="micro" tone="dim" class="tabular-nums">
          {props.columns.length} · max {usd(max())}
        </Text>
      </div>
    </div>
  );
}
