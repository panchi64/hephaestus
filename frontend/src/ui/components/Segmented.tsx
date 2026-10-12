import { For, splitProps, type JSX } from "solid-js";
import { cx } from "../cx";
import { Icon } from "../primitives/Icon";
import type { IconName } from "../icons/registry";
import { Tooltip } from "./Tooltip";

export interface SegmentedOption<T extends string> {
  value: T;
  label: string;
  icon?: IconName;
  /** Hover/focus explanation of what picking this does. */
  description?: string;
  /** The tooltip, when it should say more than `description` — a shortcut. */
  hint?: string;
}

export interface SegmentedProps<T extends string> {
  options: SegmentedOption<T>[];
  value: T;
  onChange: (value: T) => void;
  /** Required: a radiogroup with no name is a set of buttons to a screen
   *  reader. */
  "aria-label": string;
  /** Segments share the width equally. **Defaults to on** — a set of mutually
   *  exclusive choices is usually a control that owns its row; pass `false` for
   *  one sized by its labels. */
  fill?: boolean;
  class?: string;
}

/**
 * A row of mutually exclusive choices — one is always picked (§10.8 Controls).
 *
 * **Not `Tabs`, and the difference is what the control means.** A tab strip picks
 * which of several panels to *show*; this picks a value, and the thing it changes
 * may be nowhere near it. That is why it is a `radiogroup` and not a `tablist`,
 * and the distinction is not pedantry — it is what a screen reader announces, and
 * "tab 1 of 3" is a lie about a control that is not revealing anything.
 *
 * **The picked segment is an inverted cell** (`text-bright` fill, ground-coloured
 * text) — the console's selection for a segmented control, and never a coloured
 * pill, which would spend the accent on a control whose whole job may be
 * *choosing* the accent. Every segment carries the same 1px border (bright on the
 * picked one, transparent on the rest), so picking shifts nothing by a pixel.
 * Hover is the raised fill alone, so it can never be mistaken for the selection.
 * No radius, no shadow, no track behind the set.
 *
 * **`shadow-focus` is left to mean focus.** Focus and selection are always the
 * same segment — the roving tabindex parks on the picked one and the arrows
 * *select* as they move.
 *
 * **Roving tabindex, because a radiogroup is one stop.** Tab reaches the control
 * and the arrow keys move within it; every segment being separately tabbable is
 * the wrong model and makes a three-option control cost three tabs to pass.
 */
export function Segmented<T extends string>(
  props: SegmentedProps<T>,
): JSX.Element {
  const [local] = splitProps(props, [
    "options",
    "value",
    "onChange",
    "aria-label",
    "fill",
    "class",
  ]);
  /** The default lives here rather than at each use, so the two places that size
   *  a segment cannot disagree about what an omitted `fill` means. */
  const fills = (): boolean => local.fill !== false;

  /** Move the selection by `step`, wrapping — standard radiogroup behaviour, and
   *  it *selects* rather than merely focusing, which is what a radiogroup does. */
  const move = (step: number): void => {
    const items = local.options;
    if (items.length === 0) return;
    const at = items.findIndex((o) => o.value === local.value);
    const next = items[(at + step + items.length) % items.length];
    local.onChange(next.value);
  };

  const onKeyDown = (e: KeyboardEvent): void => {
    if (e.key === "ArrowRight" || e.key === "ArrowDown") move(1);
    else if (e.key === "ArrowLeft" || e.key === "ArrowUp") move(-1);
    else return;
    e.preventDefault();
  };

  return (
    <div
      role="radiogroup"
      aria-label={local["aria-label"]}
      class={cx("flex items-center gap-1", local.class)}
      onKeyDown={onKeyDown}
    >
      <For each={local.options}>
        {(option) => {
          const active = () => option.value === local.value;
          const segment = (
            <button
              type="button"
              role="radio"
              aria-checked={active()}
              // One stop for the whole group: the picked segment is the one Tab
              // lands on, and the arrows do the rest.
              tabindex={active() ? 0 : -1}
              onClick={() => local.onChange(option.value)}
              class={cx(
                // The border is on every segment — coloured on the picked one,
                // transparent on the rest — so the geometry is identical in
                // both states and nothing reflows when the choice moves. The
                // height is pinned to the 32px `md` control step rather than
                // left to fall out of the padding, so the edge grows inward and
                // the row does not gain 2px for wearing it (§4).
                "flex h-8 items-center justify-center gap-1.5 border px-3 text-body font-sans whitespace-nowrap transition-colors",
                // Focus is neutral and carried by a shadow, so it shifts no
                // layout and never competes with the accent (§10.8).
                "outline-none focus-visible:shadow-focus",
                fills() && "min-w-0 flex-1",
                // Inverted when picked, raised fill on hover; no hue, no shadow.
                active()
                  ? "border-bright bg-bright text-bg font-medium"
                  : "border-transparent font-normal text-dim hover:bg-raised hover:text-text",
              )}
            >
              {option.icon ? <Icon name={option.icon} size={14} /> : null}
              {option.label}
            </button>
          );
          return option.description ? (
            <Tooltip
              delay={600}
              side="bottom"
              label={option.hint ?? option.description}
              class={cx(fills() && "min-w-0 flex-1")}
            >
              {segment}
            </Tooltip>
          ) : (
            segment
          );
        }}
      </For>
    </div>
  );
}
