import { Show, createEffect, createSignal, untrack, type JSX } from "solid-js";
import { cx } from "../cx";

export interface CollapseProps {
  /** Whether the region is open. Changing it animates; mounting does not. */
  open: boolean;
  /** Layout glue for the collapsing container. */
  class?: string;
  children: JSX.Element;
}

/**
 * **A region that opens and closes over its own height**, without the caller
 * knowing what that height is.
 *
 * Use it wherever content genuinely comes and goes — a status line that has
 * something to say only while work is running, a panel revealing, a summary that
 * appears once there is something to summarize. The alternative is content
 * vanishing, which takes everything below it with it: a jump the reader has to
 * re-find their place after, and the single most common reason a screen "feels
 * like it redrew".
 *
 * Two details it gets right that a naive version does not:
 *
 * - **It leaves nothing behind.** Once closed, it unmounts — but only *after*
 *   the closing transition ends. A collapsed element that stays mounted is
 *   invisible and still consumes its parent's `gap`, so a spaced stack keeps a
 *   hole exactly where the thing used to be.
 * - **It does not animate on mount.** Transitions never run on initial render,
 *   so a region that mounts open simply is open. That is what keeps a long
 *   transcript still on load instead of unfolding a hundred regions at once
 *   (§8 — nothing animates that the operator is not watching).
 *
 * **Use `Reveal` instead when the region mounts at the moment it opens.** This
 * animates on a transition, and a transition needs a previous computed value —
 * an element that did not exist a moment ago has none, so it renders at its end
 * state and appears instantly. Forcing a style flush between the two is supposed
 * to fix that and does not reliably. `Reveal` animates on a keyframe, which
 * carries its own start and therefore plays on mount; that is why the View
 * panel uses it. Reach for `Collapse` when the region is already on screen and
 * has to give its space back — which is the case it is written for and the only
 * one exercised today.
 */
export function Collapse(props: CollapseProps): JSX.Element {
  // Mount on first open and stay mounted through the close, so the closing
  // transition has something to run on.
  const [mounted, setMounted] = createSignal(props.open);

  /* What the DOM currently reflects, which lags `props.open` on the opening
     edge — and that lag is the entire reason opening animates at all.

     A transition needs a *previous* computed value. A region that opens in
     response to a click mounts at that moment, so rendering it open immediately
     means the browser's first computed style for the element is the open one:
     there is nothing to transition from, and it appears instantly. So it is
     mounted closed, the closed style is forced to compute, and only then does it
     open.

     The forced read is what makes this reliable — waiting a frame is not enough
     and is not deterministic. `offsetWidth` flushes pending style and layout, so
     the closed value is genuinely committed before the open one is set, which is
     exactly the condition a transition needs.

     Mounting already open — a page loading with the panel out — is the one case
     that must NOT animate, and it doesn't: `shown` starts at `props.open`, so
     there is no edge to cross. That is what keeps a transcript of collapsed
     regions still on load. */
  const [shown, setShown] = createSignal(props.open);

  /* Whether the region has *arrived* — open and no longer travelling.

     The clip is scaffolding for the animation, not for the open state: while the
     grid row is between `0fr` and `1fr` the content is taller than its track and
     has to be cut. Once it lands, the track is exactly the content's height, and
     the clip starts cutting the one thing that legitimately lives outside a
     child's border box — the 1px ring `--shadow-1` paints around every card. The
     symptom is a card losing its right edge, and the last card in a run losing
     its bottom edge, which reads as a rendering defect rather than as a border.

     So the clip lifts here and nowhere else. Mounting already open never
     transitions, so this starts at `props.open` for the same reason `shown`
     does; closing drops it synchronously, before the height starts moving. */
  const [settled, setSettled] = createSignal(props.open);
  let host: HTMLDivElement | undefined;
  createEffect(() => {
    if (!props.open) {
      setShown(false);
      setSettled(false);
      return;
    }
    setMounted(true);
    if (untrack(shown)) return;
    // The element is inserted synchronously by the update above; the microtask
    // runs after that and before paint.
    queueMicrotask(() => {
      if (!host) return;
      void host.offsetWidth;
      setShown(true);
    });
  });

  return (
    <Show when={mounted()}>
      <div
        ref={(el) => (host = el)}
        class={cx("heph-collapse", props.class)}
        data-closed={shown() ? undefined : ""}
        data-settled={settled() ? "" : undefined}
        onTransitionEnd={(e) => {
          // Guard on the property AND the target: the content's own opacity and
          // filter transitions run alongside this one, and a child's transitions
          // bubble up here too. Unmounting on any of those would cut the region
          // short — or worse, while it is still open.
          if (e.target !== e.currentTarget) return;
          if (e.propertyName !== "grid-template-rows") return;
          if (props.open) setSettled(true);
          else setMounted(false);
        }}
      >
        <div>{props.children}</div>
      </div>
    </Show>
  );
}
