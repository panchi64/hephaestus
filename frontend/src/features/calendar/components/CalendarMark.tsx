import type { JSX } from "solid-js";
import { cx } from "~/ui";
import { calendarMarkPattern, type Calendar } from "../model";

const PATTERN_CLASS = {
  filled: "bg-bright",
  hollow: "border border-bright",
  hatched:
    "border border-bright bg-[repeating-linear-gradient(45deg,currentColor_0_1px,transparent_1px_3px)]",
  crossed:
    "border border-bright bg-[linear-gradient(135deg,currentColor_50%,transparent_50%)]",
} as const;

/** Small square whose fill pattern identifies a calendar without colour. */
export function CalendarMark(props: {
  tone: Calendar["tone"];
  class?: string;
}): JSX.Element {
  return (
    <span
      aria-hidden="true"
      class={cx(
        "inline-block size-2 shrink-0 text-bright",
        PATTERN_CLASS[calendarMarkPattern(props.tone)],
        props.class,
      )}
    />
  );
}
