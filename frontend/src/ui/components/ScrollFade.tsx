import { type JSX } from "solid-js";
import { cx } from "../cx";

/** Which edge of the block the fade sits on. */
export type ScrollFadeEdge = "top" | "bottom";

/** The ground the fade starts from — the surface the clamped block sits on. */
export type ScrollFadeGround = "bg" | "surface" | "sunken" | "raised";

export interface ScrollFadeProps {
  edge: ScrollFadeEdge;
  /** Default `bg`. */
  ground?: ScrollFadeGround;
  class?: string;
}

/**
 * "There is more below" over the foot of a clamped block: a short plain gradient from
 * `ground`, inside the block, no blur (`.heph-clamp-fade`). Mount it as the last child
 * of the `relative overflow-hidden` block.
 *
 * Decoration only: hidden from assistive tech and transparent to the pointer, since it
 * lies over live content.
 */
export function ScrollFade(props: ScrollFadeProps): JSX.Element {
  return (
    <div
      aria-hidden="true"
      data-edge={props.edge}
      data-ground={props.ground ?? "bg"}
      class={cx("heph-clamp-fade", props.class)}
    />
  );
}
