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
  /** `pill` (default): rounded segments, the picked one raised. `ruled`: full-bleed
   *  ruled mono cells with the picked one INVERTED — the rail's mode bar, the one
   *  strong element at its head. Icons are dropped there; the word is the control. */
  variant?: "pill" | "ruled";
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
 * **The picked segment is a lifted slab with its own edge — never a coloured
 * pill**, which would spend the accent on a control whose whole job may be
 * *choosing* the accent. It is separated on four neutral axes at once: it fills
 * to `surface-raised`, it is drawn at `line-strong` — the token's documented use
 * is "active control outlines" — it is the only one at medium weight, and it is
 * the only one at `text-bright`.
 *
 * **The edge is what makes it survive both modes.** The two modes carry depth
 * differently (§6): Ink steps up in surface value, which against pure black is
 * legible on its own, while Paper's `surface-raised` is a 4% tint on white — a
 * fill alone all but vanished in Paper, and a `shadow-1` cast at 5% there would
 * not have rescued it. A hairline is the one instrument that lands in both, and
 * §7 licenses it for
 * exactly this case: a control's own edge, where the border *is* the affordance.
 * The unpicked segments carry the same border transparent, so the ring costs no
 * layout and picking one shifts nothing by a pixel.
 *
 * It also keeps hover and selection apart. Hover fills to `surface-raised` too,
 * so with the fill as the only mark a hovered segment wore the selected one's
 * costume; the edge belongs to the selection alone.
 *
 * No track behind the set: `surface-sunken` is the fill of a turn in a
 * transcript, not a groove for a control to sit in, and a second surface under
 * the segments makes a small control read as a component with a frame.
 *
 * **`shadow-focus` is left to mean focus.** §7 offers it as part of a selected
 * state, but here focus and selection are always the same segment — the roving
 * tabindex parks on the picked one and the arrows *select* as they move — so a
 * halo worn at rest would be a halo that never appears, and tabbing into the
 * control would show the operator nothing.
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
    "variant",
    "class",
  ]);
  const ruled = (): boolean => local.variant === "ruled";

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
      class={cx(
        ruled() ? "ody-rail-modes" : "flex items-center gap-1",
        local.class,
      )}
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
              class={
                ruled()
                  ? "ody-rail-mode"
                  : cx(
                      // The border is on every segment — coloured on the picked one,
                      // transparent on the rest — so the geometry is identical in
                      // both states and nothing reflows when the choice moves. The
                      // height is pinned to the 32px `md` control step rather than
                      // left to fall out of the padding, so the edge grows inward and
                      // the row does not gain 2px for wearing it (§4).
                      "flex h-8 items-center justify-center gap-1.5 rounded-ctl border px-3 text-body font-sans whitespace-nowrap transition-colors",
                      // Focus is neutral and carried by a shadow, so it shifts no
                      // layout and never competes with the accent (§10.8).
                      "outline-none focus-visible:shadow-focus",
                      fills() && "min-w-0 flex-1",
                      // Fill, edge, weight, brightness — four neutral axes, and no
                      // hue. Four is the whole set: there is no shadow.
                      //
                      // `shadow-1` used to sit under the picked segment as a resting
                      // lift, and it was wrong twice. §7 ranks the ways to separate
                      // two things, and the shadow's folded hairline ring is offered
                      // at rank 3 *so that* a surface can drop its border at rank 4 —
                      // it is the alternative to an edge, not a companion to one. A
                      // segment carrying both drew its hairline twice, and a doubled
                      // hairline on a 3px corner is what read as a soft edge in a
                      // system whose corners are meant to be machined.
                      //
                      // The second fault is the register. Shadows separate layers
                      // (§6), and this control sits in the rail as one row among the
                      // thread rows, the area rows and the footer — none of which is
                      // lifted. It was the only shadow in the whole column, so it
                      // read as a chip floating over the rail rather than part of it.
                      active()
                        ? "border-line-strong bg-raised text-bright font-medium"
                        : "border-transparent font-normal text-dim hover:bg-raised hover:text-text",
                    )
              }
            >
              {option.icon && !ruled() ? (
                <Icon name={option.icon} size={14} />
              ) : null}
              {option.label}
            </button>
          );
          return option.description ? (
            <Tooltip
              delay={600}
              side="bottom"
              label={option.hint ?? option.description}
              class={cx(!ruled() && fills() && "min-w-0 flex-1")}
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
