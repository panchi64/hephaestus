import { Dynamic } from "solid-js/web";
import { createMemo, type JSX } from "solid-js";
import { cx } from "../cx";
import { icons, type IconEntry, type IconName } from "../icons/registry";

export interface IconProps {
  name: IconName;
  /** Pixel size of the square box. Defaults to 16 (the icon grid unit). */
  size?: number;
  /** Stroke width. Defaults to 1.5 per the design system. */
  stroke?: number;
  class?: string;
  "aria-label"?: string;
}

/**
 * Renders a registry icon as a 1.5px stroke SVG in currentColor. Color comes
 * from the surrounding text color (a token), never a hardcoded value.
 *
 * Glyphs live on either the native 16px grid (bespoke) or Iconoir's 24px grid.
 * The `stroke` prop is expressed in 16px-grid units and scaled to the glyph's
 * grid, so every icon renders at the same visual stroke weight regardless of
 * which grid it was authored on.
 */
export function Icon(props: IconProps): JSX.Element {
  const size = () => props.size ?? 16;
  // A name can arrive from runtime data despite the type, and a missing glyph must
  // leave an empty box rather than throw and take the surrounding render down.
  const entry = createMemo((): IconEntry | undefined => {
    if (Object.hasOwn(icons, props.name)) return icons[props.name];
    if (import.meta.env.DEV)
      console.warn(`Icon: no registry entry for ${String(props.name)}`);
    return undefined;
  });
  const grid = () => {
    const e = entry();
    return e === undefined || typeof e === "function" ? 16 : e.viewBox;
  };
  const glyph = () => {
    const e = entry();
    return e === undefined || typeof e === "function" ? e : e.glyph;
  };
  return (
    <svg
      width={size()}
      height={size()}
      viewBox={`0 0 ${grid()} ${grid()}`}
      fill="none"
      stroke="currentColor"
      stroke-width={((props.stroke ?? 1.5) * grid()) / 16}
      stroke-linecap="round"
      stroke-linejoin="round"
      class={cx("inline-block shrink-0", props.class)}
      role={props["aria-label"] ? "img" : "presentation"}
      aria-label={props["aria-label"]}
      aria-hidden={props["aria-label"] ? undefined : "true"}
    >
      <Dynamic component={glyph()} />
    </svg>
  );
}
