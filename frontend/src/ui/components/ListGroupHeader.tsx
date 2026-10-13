import { Show, splitProps, type JSX } from "solid-js";
import { cx } from "../cx";
import { Text } from "../primitives/Text";

export interface ListGroupHeaderProps {
  label: string;
  /** `micro` (default): the dim micro heading of a grouped option list. `plate`: the
   *  console register's plate — mono uppercase, a count, and a hairline running out to
   *  the edge — for the rail's groups. `section`: the plate one step brighter and
   *  without the hairline, heading a whole list (`RECENTS · 24`) with an `aside` at its
   *  right end. */
  variant?: "micro" | "plate" | "section";
  /** How many rows the heading names. Plate variants only. */
  count?: number;
  /** Something at the heading's right end — a filter, a toggle. Plate variants only. */
  aside?: JSX.Element;
  class?: string;
}

/** The heading that names a run of rows inside a list — a grouped option list, a rail
 *  group. Not a `Panel`: it sits *within* one scroll body between two runs of rows, so
 *  it carries no ground of its own. */
export function ListGroupHeader(props: ListGroupHeaderProps): JSX.Element {
  const [local] = splitProps(props, [
    "label",
    "variant",
    "count",
    "aside",
    "class",
  ]);
  return (
    <Show
      when={local.variant === "plate" || local.variant === "section"}
      fallback={
        <div class={cx("px-3 pb-0.5 pt-1.5", local.class)}>
          <Text variant="micro" tone="dim">
            {local.label}
          </Text>
        </div>
      }
    >
      <div
        class={cx("heph-list-plate", local.class)}
        data-rule={local.variant === "plate" ? "" : undefined}
        data-tone={local.variant === "section" ? "section" : undefined}
      >
        <span class="heph-list-plate-label shrink-0">{local.label}</span>
        <Show when={local.count !== undefined}>
          <Show when={local.variant === "section"}>
            <span aria-hidden="true">·</span>
          </Show>
          <span class="heph-list-plate-count">{local.count}</span>
        </Show>
        <Show when={local.aside}>
          <span class="heph-list-plate-aside">{local.aside}</span>
        </Show>
      </div>
    </Show>
  );
}
