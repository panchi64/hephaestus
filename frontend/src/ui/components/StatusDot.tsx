import { type JSX } from "solid-js";
import { cx } from "../cx";
import { type TextTone } from "../primitives/Text";

/** Semantic status. Each maps to exactly one accent (§4 color discipline). */
export type Status =
  "idle" | "live" | "nominal" | "warn" | "alert" | "info" | "queue";

/** The one Status → accent mapping. Every status indicator (the StatusFlag chip,
 *  the system strip, fallback-chain rows) reads from here so the color of a state
 *  never forks across surfaces. */
export const statusTone: Record<Status, TextTone> = {
  idle: "dim",
  live: "nominal",
  nominal: "nominal",
  warn: "warn",
  alert: "alert",
  info: "info",
  queue: "queue",
};

export interface StatusDotProps {
  /** Drives the accent. Defaults to idle (neutral, at rest). */
  status?: Status;
  /** Hard-stepped pulse to signal live activity (cursor-blink family, §8). */
  pulse?: boolean;
  /** Mark form. `dot` is the inline 6px square that sits beside a label; `square`
   *  is the 8px block the nav rail uses to flag ambient activity on a row you
   *  aren't reading — square by default per §2, and larger because it has to be
   *  legible on its own, with no adjacent text to anchor it. */
  shape?: "dot" | "square";
  /** Accessible name. Omit for a mark that merely restates adjacent text (the
   *  default: `aria-hidden`, since §4 already requires a label beside it). */
  label?: string;
  class?: string;
}

/** The shared state mark: one semantic accent, in the dot or square form. The
 *  single source for every health/status indicator so the state→color mapping
 *  never forks — `bg-current` over the tone's text color keeps mark and label in
 *  sync. */
export function StatusDot(props: StatusDotProps): JSX.Element {
  return (
    <span
      class={cx(
        "inline-block shrink-0 bg-current",
        props.shape === "square" ? "size-2" : "size-1.5",
        `text-${statusTone[props.status ?? "idle"]}`,
        props.pulse && "heph-pulse",
        props.class,
      )}
      aria-label={props.label}
      aria-hidden={props.label ? undefined : "true"}
    />
  );
}
