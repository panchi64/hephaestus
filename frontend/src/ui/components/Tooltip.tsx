import { Show, createSignal, onCleanup, splitProps, type JSX } from "solid-js";
import { Portal } from "solid-js/web";
import { cx } from "../cx";
import { computeTipPosition, type TipSide } from "./tooltipPlacement";

export interface TooltipProps {
  /** Tooltip text. */
  label: string;
  /** Edge to place the tip. Default top — flipped to the opposite edge when that
   *  side has no room. */
  side?: TipSide;
  /** Hover-intent delay before the tip shows, in ms. Default 0 (instant). */
  delay?: number;
  class?: string;
  children: JSX.Element;
}

/* A tooltip explains something to the operator, so it is the interface voice
   (§2): sans, sentence case, on a raised surface with the shadow's own hairline
   rather than a border of its own.

   `max-w` + `whitespace-normal`: the tip soft-wraps at a readable measure instead
   of running out of the window as one line. Short labels — the numeric readouts
   under the composer — never reach the cap and so still render on a single line;
   only a real sentence wraps, which is the point. `break-words` is the backstop
   for the one thing wrapping can't help with, an unbroken token (a long path, a
   model id) wider than the cap. */
const tipChrome =
  "pointer-events-none max-w-64 bg-raised px-2 py-1 text-label font-sans whitespace-normal break-words text-bright shadow-1";

/** Hover/focus tooltip.
 *
 *  **Always portaled to `document.body` and positioned `fixed`** — the same move,
 *  and the same reasoning, as `Popover`. It used to default to an `absolute` child
 *  of its own trigger and portal only when a caller passed `float`, which put the
 *  burden of knowing about clipping on all 32 call sites: any tip inside a
 *  scrolling or `overflow-hidden` ancestor — the transcript, the readout line under
 *  the composer, a panel — was cut off by it, and whether it happened to be cut off
 *  was a property of where the component was mounted rather than of anything the
 *  caller said. A portal escapes every ancestor's overflow and stacking context;
 *  the placement then flips and clamps against the viewport, so a tip near an edge
 *  moves instead of overflowing. */
export function Tooltip(props: TooltipProps): JSX.Element {
  const [local] = splitProps(props, [
    "label",
    "side",
    "delay",
    "class",
    "children",
  ]);

  let ref: HTMLSpanElement | undefined;
  let tipRef: HTMLSpanElement | undefined;
  let timer: number | undefined;
  const [shown, setShown] = createSignal(false);
  const [pos, setPos] = createSignal<{ top: number; left: number } | null>(
    null,
  );

  const measure = (): void => {
    if (!ref) return;
    // Before the tip has rendered there is no size to centre or flip on, so the
    // first pass places it from a zero box and the tip's own mount corrects it —
    // while `visibility` still hides it, so the correction is never seen.
    const tip = tipRef?.getBoundingClientRect() ?? null;
    setPos(
      computeTipPosition({
        anchor: ref.getBoundingClientRect(),
        tip: tip && { width: tip.width, height: tip.height },
        viewport: { width: window.innerWidth, height: window.innerHeight },
        side: local.side,
      }),
    );
  };

  const open = () => {
    window.clearTimeout(timer);
    timer = window.setTimeout(() => {
      setShown(true);
      measure();
    }, local.delay ?? 0);
  };
  const close = () => {
    window.clearTimeout(timer);
    setShown(false);
    setPos(null);
  };
  onCleanup(() => window.clearTimeout(timer));

  return (
    <span
      ref={ref}
      class={cx("relative inline-flex", local.class)}
      onMouseEnter={open}
      onMouseLeave={close}
      onFocusIn={open}
      onFocusOut={close}
    >
      {local.children}
      <Show when={shown()}>
        <Portal>
          <span
            ref={(el) => {
              tipRef = el;
              // Measure once the tip exists, before paint, so it never visibly
              // jumps from the provisional position to the placed one.
              queueMicrotask(measure);
            }}
            role="tooltip"
            class={cx(tipChrome, "fixed z-50")}
            style={{
              top: `${pos()?.top ?? 0}px`,
              left: `${pos()?.left ?? 0}px`,
              // Until the first real measure lands the tip would flash at 0,0.
              visibility: pos() ? "visible" : "hidden",
            }}
          >
            {local.label}
          </span>
        </Portal>
      </Show>
    </span>
  );
}
