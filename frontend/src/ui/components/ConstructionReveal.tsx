import { Show, splitProps, type JSX } from "solid-js";
import { cx } from "../cx";
import { useGatedMount } from "./useGatedMount";

export interface ConstructionRevealProps {
  /** Open/close gate. Unlike `Reveal`, this is required — the whole component
   *  is a region the caller opens and closes, and there is no ungated form. */
  when: boolean;
  /** Which corner the single `+` starts from and returns to. Default
   *  `top-right`: the View opens from the right of the screen, so the gesture
   *  should read as coming *from* the edge the panel arrives at. */
  origin?: "top-right" | "top-left";
  /** Layout glue for the wrapper — how the region sits in its parent. */
  class?: string;
  /** Layout glue for the element that holds the children and carries the
   *  surface fade. Needed because that element sits between the wrapper and the
   *  content, so a caller laying its children out with flex has to put that
   *  flex *here*, not on `class`. */
  contentClass?: string;
  children: JSX.Element;
}

/** The four straddle offsets, in one place so they cannot drift apart — 6px is
 *  half the 12px mark, which is what puts a mark's centre exactly on its corner.
 *
 *  A table rather than four literals scattered through the markup, because that
 *  is precisely how this broke once: the vertical pair was moved to `-1.5` and
 *  the horizontal pair left at `-1`, so every mark straddled 6px on one axis and
 *  4px on the other. Nothing failed — it just looked very slightly wrong, which
 *  is the hardest kind of bug to see.
 *
 *  They must also stay **literal class names**. Tailwind generates a utility only
 *  if it can see the whole string in the source, so composing one by template
 *  (`` `-${side}-1.5` ``) would silently emit nothing and drop every mark to its
 *  static position in the corner it came from. */
const OFFSET = {
  top: "-top-1.5",
  bottom: "-bottom-1.5",
  left: "-left-1.5",
  right: "-right-1.5",
} as const;

/** One corner mark: two hairlines crossing.
 *
 *  Spans rather than a glyph, so the stroke is exactly one device hairline at
 *  any zoom; and spans rather than SVG, because the panel's width is a runtime
 *  inline style and its height is `100%` — an SVG stretched to track that would
 *  need `preserveAspectRatio="none"`, which scales the two strokes by different
 *  factors and gives the `+` visibly unequal arms.
 *
 *  `bg-dim`, not `bg-line`: a mark is not a border. `--line` is tuned to be the
 *  faintest thing that still separates two surfaces, and at 1px on a dark panel
 *  it disappears — a thin stroke reads dimmer than a filled area of the same
 *  colour. These are registration marks and are meant to be seen.
 *
 *  12px against the `-1.5` (6px) offsets below, so the mark straddles the corner
 *  it names with its centre exactly on it. Positioning is by inset, never by
 *  `-translate-x-1/2`: the carriers animate `transform`, and a centring
 *  transform here would be overwritten by the travel. */
function Mark(props: { class?: string }): JSX.Element {
  return (
    <span class={cx("absolute block h-3 w-3", props.class)}>
      <span class="absolute top-1/2 left-0 h-px w-full -translate-y-1/2 bg-dim" />
      <span class="absolute top-0 left-1/2 h-full w-px -translate-x-1/2 bg-dim" />
    </span>
  );
}

/**
 * **A region that draws its own frame before it fills.** The View panel's
 * arrival: a single `+` at the origin corner splits, one half travelling along
 * the top edge with a rule drawn between them, then both drop down the sides
 * with a rule closing the bottom — and the glass surface resolves inside the
 * frame that has just been described. Closing runs the gesture in reverse, so
 * the panel is taken apart rather than switched off.
 *
 * It is a sibling of `Reveal`, not a variant of it. `Reveal` says *content
 * materialized here*; this says *a place was made, and then filled*. That is
 * worth a second component only because the View is a region the operator
 * deliberately opens — a fade would say it had always been there and the light
 * had merely come up.
 *
 * **It is also the system's one frosted framed panel**, and the 404 takes it with a
 * constant-true `when` for exactly that: marks, rules and `.heph-glass` on the region
 * between them. Nothing else may reproduce that combination — `RegistrationFrame`
 * draws marks and deliberately no surface — because two of them would be two things
 * to keep in step. The 404 is the one consumer that is not *opened*; it has no exit,
 * and what it borrows from the gesture is a plate assembling itself in front of
 * someone who arrived by mistake.
 *
 * **The budget is `--motion-stage` (320ms), and that is not a new exception.**
 * §8 grants the stage token to "a whole region arriving or leaving"; the View's
 * previous `Reveal` already ran at it, and this replaces that reveal. The phases
 * overlap by 20ms on purpose: two strictly sequential beats inside 320ms read as
 * a stutter, where an overlap reads as one continuous L-shaped gesture. The
 * timeline lives in `theme.css` under THE CONSTRUCTION REVEAL — this file owns
 * the geometry, that file owns the choreography.
 *
 * **The content is one fade, never a stagger.** §8 forbids staggered cascades,
 * and this is one region arriving rather than a list of things: the surface and
 * everything inside it resolve together, on a single animation.
 *
 * **Every moving part rides a full-size carrier, not the mark itself.** A
 * percentage `translate` resolves against the *animated element's own* border
 * box, so translating a 12px mark by 100% moves it 12px — not across the panel.
 * Each mark therefore sits at its destination inside an `inset-0` carrier, and
 * the carrier is what travels: 100% of the carrier is 100% of the panel, which
 * is the distance actually wanted, at any width the resize handle produces.
 */
export function ConstructionReveal(
  props: ConstructionRevealProps,
): JSX.Element {
  const [local] = splitProps(props, [
    "when",
    "origin",
    "class",
    "contentClass",
    "children",
  ]);
  const gate = useGatedMount(() => local.when);
  const leftOrigin = () => local.origin === "top-left";

  /* `--frame-from-x` is where the travelling carrier starts, signed by which
     corner the gesture comes from; `--frame-origin-x` is the `transform-origin`
     the horizontal rules unfurl from, so both grow away from that same corner.
     `--frame-from-y` is the drop, always downward from above. */
  const vars = (): JSX.CSSProperties => ({
    "--frame-from-x": leftOrigin() ? "-100%" : "100%",
    "--frame-origin-x": leftOrigin() ? "left" : "right",
    "--frame-from-y": "-100%",
  });

  /** The corner the gesture starts at, and the one it travels to. */
  const near = () => (leftOrigin() ? OFFSET.left : OFFSET.right);
  const far = () => (leftOrigin() ? OFFSET.right : OFFSET.left);

  return (
    <Show when={gate.mounted()}>
      <div
        /* `relative`, and deliberately NOT `isolate`. An `isolation: isolate`
           ancestor becomes the *backdrop root* for anything inside it, so the
           glass below would have blurred only what this wrapper itself paints —
           which is nothing — instead of the page behind it. The frosted effect
           dies silently under it: the fill still tints, so it looks like a
           slightly lighter surface rather than like broken glass, which is
           exactly how it read. The frame's `z-10` is enough on its own. */
        class={cx("heph-frame relative", local.class)}
        style={vars()}
        /* `data-ready` releases the whole timeline at once — every animation in
           theme.css is scoped under it, so until it appears nothing is running
           and the region is held invisible. Withholding the animations is what
           makes the wait work: the marks carry their classes statically, so
           anything not scoped would start on mount and could finish before the
           tree behind it had even settled. */
        data-ready={gate.ready() ? "" : undefined}
        data-closed={gate.closing() ? "" : undefined}
        /* `.heph-frame` carries a lifecycle span — an invisible animation lasting
           exactly as long as the whole sequence — so `animationend` fires on
           THIS element at the true end. `useGatedMount` ignores everything that
           merely bubbles up from the marks, the rules, or the content, which is
           what stops the region being torn out on whichever phase happens to
           finish first. See the note in theme.css. */
        onAnimationEnd={gate.onAnimationEnd}
      >
        {/* THE SURFACE IS THE FRAMED AREA — the region between the marks, and
            nothing wider. It is a sibling of the content rather than a fill on
            whatever the caller renders inside, because a card with its own
            rounded corners and its own shadow sitting *around* the frame is a
            second container: the frosted area then reads as a pane the marks are
            decorating rather than as the pane the marks describe. Square, no
            radius, no elevation. `inset-1.5` puts it on exactly the box the
            rules draw. */}
        <div class="heph-frame-surface heph-glass absolute inset-1.5" />

        {/* The frame, INSET 6px into the region rather than drawn on its edge.

            Flush, it landed a hairline from whatever edge was already there, and
            two rules a few pixels apart read as a mistake rather than as a
            frame. The fix for the specific offender is elsewhere — a splitter
            beside a self-framing panel takes `divider="hover"` — but the inset
            is what keeps the frame independent of its surroundings generally.

            It also stops the marks being clipped. They straddle their corners,
            so flush against a region pinned to the viewport — the full-screen
            sheet — their outer halves fell off the screen entirely. At
            `inset-1.5` with `-1.5` offsets the outer edge lands exactly on the
            region's own edge: as far out as it can go and still be whole.

            Never interactive, and above the surface so the rules and marks sit
            on the glass rather than under it. */}
        <div
          class="pointer-events-none absolute inset-1.5 z-10"
          aria-hidden="true"
        >
          {/* Phase 1 — the origin mark is simply there; its twin travels the
              top edge with a rule drawn between them.

              Every mark carries `z-10` so it paints ON TOP of the rules. These
              are all positioned children with `z-index: auto`, so without it
              they stack in DOM order and each rule — declared after the marks
              of its own phase — laid a hairline straight across the middle of
              the `+`, breaking the glyph exactly where the two strokes cross. */}
          <Mark class={cx("z-10", OFFSET.top, near())} />
          <div class="heph-frame-mark-a absolute inset-0 z-10">
            <Mark class={cx(OFFSET.top, far())} />
          </div>
          <span class="heph-frame-rule-top absolute top-0 right-0 left-0 h-px bg-line" />

          {/* Phase 2 — both marks drop to the bottom edge, the sides close
              behind them, and the bottom rule completes the frame. */}
          <div class="heph-frame-mark-b absolute inset-0 z-10">
            <Mark class={cx(OFFSET.bottom, near())} />
            <Mark class={cx(OFFSET.bottom, far())} />
          </div>
          <span class="heph-frame-rule-side absolute top-0 bottom-0 left-0 w-px bg-line" />
          <span class="heph-frame-rule-side absolute top-0 right-0 bottom-0 w-px bg-line" />
          <span class="heph-frame-rule-bottom absolute right-0 bottom-0 left-0 h-px bg-line" />
        </div>

        {/* `relative` so the content paints above the absolutely-positioned
            surface behind it, and `p-1.5` so it sits INSIDE the framed box
            rather than spilling the 6px out to the region's own edge. The
            padding is also what keeps the wrapper sized by its content: the
            surface and the frame are both absolute and contribute no width, so
            an aside with an explicit width still drives the column. */}
        <div
          class={cx("heph-frame-surface relative p-1.5", local.contentClass)}
        >
          {local.children}
        </div>
      </div>
    </Show>
  );
}
