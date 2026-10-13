import { Show, splitProps, type JSX } from "solid-js";
import { cx } from "../cx";
import { useGatedMount } from "./useGatedMount";

/** How the content arrives. `fade` is opacity alone — for something appearing
 *  in place, where movement would imply it came from somewhere. `rise` adds a
 *  short glide up, for something entering the screen. */
export type RevealMotion = "fade" | "rise";

const motionClass: Record<RevealMotion, string> = {
  fade: "heph-fade-in",
  rise: "heph-rise",
};

export interface RevealProps {
  /** Default `fade`. */
  motion?: RevealMotion;
  /** Glide distance in px for `rise`. Default 4 — an overlay settling. The
   *  transcript's turn arrival uses 10, which is about as far as this register
   *  goes before the movement starts reading as decoration. Ignored by `fade`. */
  distance?: number;
  /** Duration in ms. Defaults by kind: an **ungated** reveal is content arriving
   *  into a place that already exists, and paces at `--motion-base` (180ms); a
   *  **gated** one is a whole region being opened or closed, and paces at
   *  `--motion-stage` (320ms), which is what that token is for. A region moving
   *  at a control's speed reads as a flash — there is much more of it to take
   *  in, and nothing is waiting on it. */
  duration?: number;
  /** Starting blur in px. Default 3. This is what makes the arrival read as the
   *  content *materializing* rather than as a light being turned up on something
   *  that was already there. `0` opts out — worth doing for a very large surface,
   *  where blurring the whole raster for a frame or two can cost more than the
   *  effect is worth. */
  blur?: number;
  /** Gate the reveal. While false nothing is rendered; when it flips true the
   *  content mounts and animates in — and when it flips back, the content
   *  **dissolves the way it resolved** before unmounting, rather than being cut.
   *
   *  This is the only way to get an exit out of `Reveal`, and deliberately so:
   *  an ungated reveal is content arriving in a place that keeps it, where an
   *  exit animation would only delay the operator getting what they asked for.
   *  A gated one is a region the caller genuinely opens and closes, and there a
   *  hard cut is the jolt. */
  when?: boolean;
  /** Layout glue for the wrapper. */
  class?: string;
  children: JSX.Element;
}

/**
 * **The one way something in the human voice arrives.** Wraps its children in an
 * element that *materializes* — resolving out of a blur while it fades, and with
 * `rise`, settling the last few pixels into place as it does.
 *
 * The blur is the load-bearing part. Fading opacity alone reads as a light being
 * turned up on something that was already sitting there; coming out of a blur
 * reads as the thing arriving. It is the difference between revealed and made.
 *
 * Reach for it whenever the interface puts something on screen on the operator's
 * behalf: a panel revealing, a section expanding, a result landing. The register
 * is the point (§8) — anything the interface does eases, and anything the
 * *machine* does snaps, so a mono readout must never be wrapped in this. That
 * contrast is what lets the operator tell the two apart without reading a word.
 *
 * Two things about how it comes and goes:
 * - **Ungated, it never exits.** Content that simply arrives somewhere and stays
 *   should leave the instant it is no longer wanted; an exit animation there
 *   only delays the operator. Pass `when` and it gains one — see that prop.
 * - **It does not animate on every update.** The animation fires on mount. To
 *   replay it, change `when` — or key the subtree, if the content itself is what
 *   changed.
 *
 * For a case that cannot take a wrapper — a portal root, a node built by hand —
 * the underlying `.heph-fade-in` / `.heph-rise` classes are still there.
 */
export function Reveal(props: RevealProps): JSX.Element {
  const [local] = splitProps(props, [
    "motion",
    "distance",
    "duration",
    "blur",
    "when",
    "class",
    "children",
  ]);

  // The presets carry the defaults; only an explicit override is written, so a
  // Reveal with no props emits no inline style at all.
  const style = (): JSX.CSSProperties => ({
    ...(local.distance === undefined
      ? {}
      : { "--reveal-y": `${local.distance}px` }),
    // A gated reveal opens and closes a whole region, so it takes the stage
    // duration rather than a control's. Still a token, not a number — an
    // explicit `duration` overrides both.
    ...(local.duration !== undefined
      ? { "--reveal-ms": `${local.duration}ms` }
      : local.when !== undefined
        ? { "--reveal-ms": "var(--motion-stage)" }
        : {}),
    ...(local.blur === undefined ? {} : { "--reveal-blur": `${local.blur}px` }),
  });

  /* The mount/exit lifecycle — staying mounted through the exit, the two-frame
     settle before the entry starts, and unmounting only on this element's own
     animation — lives in `useGatedMount`, which `ConstructionReveal` shares.
     See that file for why each of those four behaviours is there. */
  const gate = useGatedMount(() => local.when);

  return (
    <Show when={gate.mounted()}>
      <div
        class={cx(
          // Held invisible until the tree has settled, then the motion class
          // arrives and its animation plays from there.
          gate.ready()
            ? motionClass[local.motion ?? "fade"]
            : "heph-reveal-hold",
          local.class,
        )}
        style={style()}
        data-closed={gate.closing() ? "" : undefined}
        onAnimationEnd={gate.onAnimationEnd}
      >
        {local.children}
      </div>
    </Show>
  );
}
