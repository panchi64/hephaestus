import {
  createSignal,
  onCleanup,
  onMount,
  splitProps,
  type JSX,
} from "solid-js";
import { cx } from "../cx";

export interface MarqueeProps {
  /** Scroll speed in px/s. Default 40. */
  speed?: number;
  class?: string;
  children: JSX.Element;
}

/**
 * Horizontally scrolls its content only when it overflows the available width,
 * revealing otherwise-clipped text; static when it fits. Pair with a flexible,
 * `min-w-0` parent so it claims the leftover row space instead of forcing
 * horizontal overflow. Respects prefers-reduced-motion (falls back to static
 * clip). The keyframe (`heph-marquee`) lives in the design-system CSS.
 */
export function Marquee(props: MarqueeProps): JSX.Element {
  const [local] = splitProps(props, ["speed", "class", "children"]);
  let outer: HTMLDivElement | undefined;
  let inner: HTMLDivElement | undefined;
  const [overflow, setOverflow] = createSignal(0);
  const [duration, setDuration] = createSignal(0);

  const measure = () => {
    if (!outer || !inner) return;
    const diff = inner.scrollWidth - outer.clientWidth;
    if (diff > 1) {
      setOverflow(diff);
      // Round trip at a constant px/s pace, with a sane floor.
      setDuration(Math.max((diff / (local.speed ?? 40)) * 2, 4));
    } else {
      setOverflow(0);
    }
  };

  onMount(() => {
    measure();
    const ro = new ResizeObserver(measure);
    if (outer) ro.observe(outer);
    if (inner) ro.observe(inner);
    onCleanup(() => ro.disconnect());
  });

  return (
    <div ref={outer} class={cx("overflow-hidden", local.class)}>
      <div
        ref={inner}
        class={cx(
          "w-max whitespace-nowrap",
          overflow() > 0 &&
            "motion-safe:animate-[heph-marquee_var(--mq-dur)_linear_infinite]",
        )}
        style={
          overflow() > 0
            ? {
                "--mq-shift": `-${overflow()}px`,
                "--mq-dur": `${duration()}s`,
              }
            : undefined
        }
      >
        {local.children}
      </div>
    </div>
  );
}
