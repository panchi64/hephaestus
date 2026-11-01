import { Show, type JSX } from "solid-js";
import { Icon, cx, type IconName } from "~/ui";
import { FRAMES, type Frame } from "../frames";

/** A schematic glyph in its frame (§8).
 *
 *  The layout box is 24×16, so a row stays 24px tall; the outline is drawn in a 24×24
 *  box over it and spills into the row's padding. */
export function Socket(props: {
  icon: IconName;
  frame?: Frame;
  dashed?: boolean;
  /** Runs the hairline from the box's left edge into the outline, for a socket an
   *  elbow lands on. */
  lead?: boolean;
  iconClass?: string;
  /** The arrival charge, put on the outline and the glyph alike. */
  charge?: string;
}): JSX.Element {
  const geometry = () => FRAMES[props.frame ?? "square"];
  return (
    <span class="relative h-4 w-6 shrink-0">
      <svg
        aria-hidden="true"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        stroke-width="1"
        /* `pointer-events-none`: the box overhangs the 16px slot into the rows above
           and below, and would otherwise take their clicks. */
        class={cx(
          "pointer-events-none absolute -top-1 left-0 size-6 overflow-visible text-line",
          props.charge,
        )}
      >
        {/* 12.5, not 12: the elbow is a 1px border whose centre is half a pixel
            down. Drawn from the outline back, so a dashed lead starts on a dash
            where it meets the frame. */}
        <Show when={props.lead}>
          <path
            d={`M${geometry().left} 12.5H0`}
            stroke-dasharray={props.dashed ? "3 3" : undefined}
          />
        </Show>
        <path
          d={geometry().d}
          stroke-dasharray={props.dashed ? "3 2" : undefined}
        />
      </svg>
      {/* Pinned to whole pixels (6, 2) rather than centred: an 11px glyph centred
          on (12, 8) lands on half pixels and blurs on a 1x display. */}
      <Icon
        name={props.icon}
        size={11}
        class={cx(
          "absolute top-0.5 left-1.5",
          props.iconClass ?? "text-dim",
          props.charge,
        )}
      />
    </span>
  );
}
