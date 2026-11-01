import { Show, splitProps, type JSX } from "solid-js";
import { cx } from "../cx";
import { Text } from "../primitives/Text";

export type PanelState = "default" | "active" | "alert";

export interface PanelProps extends JSX.HTMLAttributes<HTMLElement> {
  /** Header plate: a mono uppercase legend naming the region. Omit for a
   *  headerless panel. */
  label?: string;
  /** Right-aligned header content: status flag, count, meta. */
  meta?: JSX.Element;
  /** Emphasis state, carried by the 1px rule's colour (`active` = info, `alert` =
   *  alert) so a state change shifts no layout and nothing glows. */
  state?: PanelState;
  /** Remove the default body padding (for edge-to-edge content like tables). */
  flush?: boolean;
  /** Make the panel a full-height flex column whose body fills the remaining
   *  space — for content that must fill its height (an iframe, a scroll
   *  region). Pair with a height on `class` (e.g. `h-full`). */
  fill?: boolean;
  /** Drop the surface entirely: no fill, no rule, no card padding — the panel
   *  becomes its plate and its content, sitting directly on the page.
   *
   *  For ambient regions that should read as *behind* the interface rather than
   *  as objects on it: the home page's recent-threads list, the system strip.
   *  A card is a claim on attention; anything ambient should be bare. */
  bare?: boolean;
}

const stateRule: Record<PanelState, string> = {
  default: "border-line",
  active: "border-info",
  alert: "border-alert",
};

/** A ruled frame with an optional plate (§10.2): `surface` fill inside a 1px
 *  hairline, square, no shadow — the console register. State recolours the rule
 *  and never the geometry. */
export function Panel(props: PanelProps): JSX.Element {
  const [local, rest] = splitProps(props, [
    "label",
    "meta",
    "state",
    "flush",
    "fill",
    "bare",
    "class",
    "children",
  ]);
  return (
    <section
      class={cx(
        local.bare
          ? "bg-transparent"
          : cx("border bg-surface", stateRule[local.state ?? "default"]),
        local.fill && "flex flex-col",
        local.class,
      )}
      {...rest}
    >
      <Show when={local.label || local.meta}>
        <header
          class={cx(
            "flex items-center gap-2 pb-2",
            local.label ? "justify-between" : "justify-end",
            // Card padding is the surface's, so a bare panel doesn't pay it —
            // its plate sits flush with the content it names.
            !local.bare && "px-4 pt-3",
          )}
        >
          {/* No empty label beside a lone meta — `justify-end` keeps the meta
              on the right without a blank element holding the left. */}
          <Show when={local.label}>
            <Text variant="plate" tone="dim">
              {local.label}
            </Text>
          </Show>
          <Show when={local.meta}>{local.meta}</Show>
        </header>
      </Show>
      <div
        class={cx(
          !local.bare &&
            !local.flush &&
            (local.label || local.meta ? "px-4 pb-4" : "p-4"),
          local.fill && "min-h-0 flex-1",
        )}
      >
        {local.children}
      </div>
    </section>
  );
}
