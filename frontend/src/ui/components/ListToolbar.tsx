import { Show, splitProps, type JSX } from "solid-js";
import { cx } from "../cx";
import { Text } from "../primitives/Text";
import { Icon } from "../primitives/Icon";
import { Checkbox } from "./Checkbox";
import { Input } from "./Input";
import { Select } from "./Select";

export interface ListToolbarProps {
  // search
  query: string;
  onQueryChange: (q: string) => void;
  placeholder?: string;
  // sort (omit to hide the sort control)
  sortKey?: string;
  sortOptions?: { value: string; label: string }[];
  onSortChange?: (key: string) => void;
  dir?: "asc" | "desc";
  onToggleDir?: () => void;
  // result count, e.g. "12 / 40"
  count?: number;
  total?: number;
  // select-all control (shown left of search when onToggleAll is provided)
  allSelected?: boolean;
  onToggleAll?: () => void;
  // selection / bulk-action strip (shown only while selectedCount > 0)
  selectedCount?: number;
  bulkActions?: JSX.Element;
  onClearSelection?: () => void;
  /** Extra inline filter controls placed between search and sort. */
  children?: JSX.Element;
  class?: string;
}

/**
 * The visual half of the list-scaling paradigm: a search field, an optional
 * sort control with a direction toggle, a live result count, and an optional
 * bulk-action strip. Pair with `createListView` (`~/lib/list`) for the state.
 *
 *   <ListToolbar
 *     query={view.query()} onQueryChange={view.setQuery}
 *     sortKey={view.sortKey()} sortOptions={view.sortOptions}
 *     onSortChange={view.setSort} dir={view.dir()} onToggleDir={view.toggleDir}
 *     count={view.count()} total={view.total()}
 *   />
 */
export function ListToolbar(props: ListToolbarProps): JSX.Element {
  const [local] = splitProps(props, [
    "query",
    "onQueryChange",
    "placeholder",
    "sortKey",
    "sortOptions",
    "onSortChange",
    "dir",
    "onToggleDir",
    "count",
    "total",
    "allSelected",
    "onToggleAll",
    "selectedCount",
    "bulkActions",
    "onClearSelection",
    "children",
    "class",
  ]);

  const showCount = () =>
    local.count !== undefined && local.total !== undefined;
  const hasSort = () => (local.sortOptions?.length ?? 0) > 0;
  const selecting = () => (local.selectedCount ?? 0) > 0;

  return (
    <div class={cx("flex flex-col", local.class)}>
      <div class="flex flex-wrap items-center gap-2">
        <Show when={local.onToggleAll}>
          <Checkbox
            checked={local.allSelected}
            onChange={() => local.onToggleAll!()}
            label="All"
          />
        </Show>
        <div class="min-w-[12rem] flex-1">
          <Input
            leading="search"
            type="search"
            value={local.query}
            placeholder={local.placeholder ?? "Search…"}
            onInput={(e) => local.onQueryChange(e.currentTarget.value)}
          />
        </div>

        {local.children}

        <Show when={hasSort()}>
          <div class="flex items-center gap-1">
            <Select
              options={local.sortOptions!}
              value={local.sortKey}
              onChange={(v) => local.onSortChange?.(v)}
              class="h-8 w-auto"
            />
            <Show when={local.onToggleDir}>
              <button
                type="button"
                onClick={() => local.onToggleDir!()}
                aria-label={
                  local.dir === "asc" ? "Sort ascending" : "Sort descending"
                }
                title={local.dir === "asc" ? "Ascending" : "Descending"}
                class="flex size-8 items-center justify-center text-dim transition-colors hover:bg-raised hover:text-bright"
              >
                <Icon
                  name="chevron-down"
                  size={14}
                  class={cx(
                    "transition-transform",
                    local.dir === "asc" && "rotate-180",
                  )}
                />
              </button>
            </Show>
          </div>
        </Show>

        <Show when={showCount()}>
          <Text variant="micro" tone="dim" class="shrink-0 tabular-nums">
            {local.count} / {local.total}
          </Text>
        </Show>
      </div>

      <Show when={selecting()}>
        <div class="mt-2 flex flex-wrap items-center gap-3 border border-line bg-raised px-3 py-2">
          <Text variant="label" tone="bright" class="shrink-0">
            {local.selectedCount} SELECTED
          </Text>
          <div class="flex flex-1 flex-wrap items-center gap-2">
            {local.bulkActions}
          </div>
          <Show when={local.onClearSelection}>
            <button
              type="button"
              onClick={() => local.onClearSelection!()}
              class="flex items-center gap-1 text-dim transition-colors hover:text-bright"
            >
              <Icon name="close" size={12} />
              <Text variant="label">Clear</Text>
            </button>
          </Show>
        </div>
      </Show>
    </div>
  );
}
