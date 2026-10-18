import { splitProps, type JSX } from "solid-js";
import { cx } from "../cx";

export interface DotFieldProps {
  /** How far down the page the falloff centres, as a CSS length. Defaults to 50%. */
  focusY?: string;
  class?: string;
}

/** The licensed moment (§11.1): a faint dot grid behind a whole page, strongest
 *  around the focal object and gone by the edges. The look lives in `.heph-dot-field`.
 *
 *  One per screen, and only where it backs the page rather than a panel: the
 *  launchpad and the 404. Never inside a container holding data, and deliberately not
 *  a prop on `EmptyState`, which is a panel's empty arm, not a screen's. Framed
 *  surfaces on a field page go glass (`.heph-framed` under `.heph-field-page`), so
 *  nothing readable sits on the dots.
 *
 *  Decorative, so `aria-hidden` and `pointer-events: none`. */
export function DotField(props: DotFieldProps): JSX.Element {
  const [local] = splitProps(props, ["focusY", "class"]);
  return (
    <div
      class={cx("heph-dot-field", local.class)}
      style={local.focusY ? { "--dot-field-y": local.focusY } : undefined}
      aria-hidden="true"
    />
  );
}
