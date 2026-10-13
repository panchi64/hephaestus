import { type JSX } from "solid-js";
import { cx } from "../cx";

export interface CaretProps {
  /** Layout/colour glue (e.g. `text-bright`); merged after the base class. */
  class?: string;
}

/** The terminal block cursor — the one permitted motion in the system (design
 *  §8): a hard-stepped blink, never a fade. Shared by every "live"/typing
 *  surface (streaming answers, the title typewriter) so the glyph and blink stay
 *  in lockstep. Decorative, so hidden from assistive tech. */
export function Caret(props: CaretProps): JSX.Element {
  return (
    <span class={cx("heph-caret", props.class)} aria-hidden="true">
      ▋
    </span>
  );
}
