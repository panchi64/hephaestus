/**
 * Following the stream — the transcript's one piece of genuinely stateful behaviour.
 *
 * Keep the view pinned to the bottom while an answer arrives, yield the moment the operator
 * scrolls up to read back, and re-attach when they scroll near the bottom again. A floating
 * control jumps back down once they have scrolled far up.
 *
 * **Why the content's size drives it.** The transcript grows in ways no message field
 * records: a stopped turn reloading with its failed call opened, an image decoding, a
 * fold animating open. So the follow observes the content element's size, and a pinned
 * view follows every growth whatever caused it.
 *
 * **Why re-attaching does not scroll.** Only growth moves the view. Snapping on
 * re-attach would take the scroll away from the operator while they were using it; the
 * next growth catches up instead.
 *
 * **Why detaching is decided by movement rather than by distance.** This used to yield
 * on position alone — within `ATTACHED_PX` of the bottom counted as attached, full stop —
 * and that made the bottom of a streaming transcript impossible to leave. A scroll-up
 * smaller than the threshold left the follow attached, so the next fragment (milliseconds
 * later, on the very next frame) put the view straight back at the bottom. The operator
 * could only escape by flicking hard enough to clear the whole threshold in one gesture,
 * which is precisely the complaint: the transcript felt locked to the newest step.
 *
 * So the rule is *any upward movement the operator made detaches it*, and the threshold
 * survives only as the way back in. The movement is read from the outcome, so a wheel, a
 * trackpad, a dragged scrollbar, a Page Up and a touch drag behave the same. Authorship is
 * read from input: the browser also moves `scrollTop` up on its own, clamping it when a
 * reloaded turn lays out shorter for a moment, and that must not let go of the follow.
 */

import { createEffect, createSignal, onCleanup, untrack } from "solid-js";

/** Scrolling back to within this many pixels of the bottom re-attaches the follow.
 *
 *  It is a *re-attachment* threshold only. It used to double as the detachment rule —
 *  anything inside it counted as attached — which is what made a small scroll-up
 *  impossible: the follow simply undid it on the next frame. Nothing reads this to
 *  decide whether to let go any more. */
const ATTACHED_PX = 80;
/** Past roughly one screenful the jump-to-latest control appears. */
const JUMP_PX = 240;
/** Upward movement under this many pixels is rounding, not a gesture.
 *
 *  Sub-pixel layout and the browser's own clamping move `scrollTop` by fractions on
 *  their own; anything above that came from the operator, however small. Two pixels is
 *  deliberately almost nothing — the point of the rewrite is that there is no longer a
 *  distance an operator has to out-scroll. */
const DRIFT_PX = 2;
/** A scroll this soon after the operator's last wheel, touch or key is theirs. Covers
 *  smooth keyboard scrolling, which keeps moving after the keydown. */
const INTENT_MS = 600;

/** Where the container was, and where it is now. */
export interface FollowReading {
  /** Whether the follow is currently attached. */
  pinned: boolean;
  /** Whether the operator is scrolling: input on the transcript just now, or a held
   *  pointer (a scrollbar drag). */
  operator: boolean;
  /** `scrollTop` as of the previous reading — or as of the follow's own last write. */
  lastTop: number;
  /** `scrollHeight` as of the previous reading. */
  lastHeight: number;
  top: number;
  height: number;
  clientHeight: number;
}

/**
 * Whether the follow should still be attached, given how the container just moved.
 *
 * Pure, and exported, because this is the whole of the behaviour worth testing and the
 * one rule that has been wrong before. Three cases, in this order:
 *
 * 1. **The operator moved the container up and it did not shrink.** Let go immediately;
 *    there is no minimum gesture. An upward move nobody made is the browser clamping.
 * 2. **We are at the bottom.** Take the follow back. This is the only way in besides the
 *    jump control and starting a turn.
 * 3. **Neither.** Leave it as it was. Growing content moves the *bottom* away rather than
 *    moving the view, so a detached transcript stays detached while an answer streams on
 *    underneath it.
 *
 * The shrink guard in case 1 exists because closing a fold can take `scrollHeight` below
 * `scrollTop`, and the browser then clamps the view upward on its own. That is the one
 * upward movement the operator did not ask for, and reading it as a gesture would drop
 * the follow every time a work log collapsed.
 */
export function nextPinned(reading: FollowReading): boolean {
  const shrank = reading.height < reading.lastHeight;
  const movedUp = reading.top < reading.lastTop - DRIFT_PX;
  if (reading.operator && !shrank && movedUp) return false;
  const distance = reading.height - reading.top - reading.clientHeight;
  if (distance < ATTACHED_PX) return true;
  return reading.pinned;
}

export interface TranscriptFollow {
  /** `ref` for the scrolling transcript container. */
  ref: (el: HTMLDivElement) => void;
  /** `ref` for the content inside the container; its growth drives the follow. */
  contentRef: (el: HTMLElement) => void;
  /** `onScroll` for the same container. */
  onScroll: () => void;
  /** True once the operator has scrolled far enough up to want a way back. */
  showJump: () => boolean;
  /** Re-attach the follow and scroll to the newest turn. */
  jumpToLatest: () => void;
  /** Bring one turn into view and **detach the follow**, answering with its element —
   *  or `null` when this transcript does not hold it.
   *
   *  The detach is the substance, not a side effect. Scrolling somewhere up-thread while
   *  the follow is still pinned does nothing the operator can see: the next growth
   *  writes `scrollTop = scrollHeight` and puts them straight back at the bottom.
   *  And it is the honest state anyway — looking at a turn above the tail is exactly what
   *  being un-pinned means, which is why the jump control appears afterwards. */
  scrollToMessage: (messageId: string) => HTMLElement | null;
  /** The container itself, for the keymap's focus targets. */
  element: () => HTMLDivElement | undefined;
}

export function createTranscriptFollow(source: {
  sending: () => boolean;
  /** The open thread; a switch re-attaches and jumps to the latest message. */
  conversationId: () => string | null;
}): TranscriptFollow {
  let scrollEl: HTMLDivElement | undefined;
  const [pinned, setPinned] = createSignal(true);
  const [showJump, setShowJump] = createSignal(false);

  // At most one scroll per frame.
  //
  // `scrollTop = scrollHeight` reads a value the browser can only answer by laying the
  // whole transcript out, then writes back into that same layout — so scheduling one per
  // *token* made every fragment of a long answer force a synchronous reflow of every
  // turn above it. A frame is the finest granularity the operator can perceive anyway,
  // and tokens arrive several to a frame, so coalescing here costs nothing visible and
  // takes the flush off the delta path.
  let frame: number | null = null;
  // The container as of the last thing that moved it — a scroll the operator made, or
  // the follow's own write. `nextPinned` compares against these to tell the two apart:
  // recording the write here is what stops the follow reading its own scroll event back
  // as movement.
  let lastTop = 0;
  let lastHeight = 0;
  let lastInput = Number.NEGATIVE_INFINITY;
  let pointerHeld = false;
  const noteInput = () => {
    lastInput = performance.now();
  };
  const holdPointer = () => {
    pointerHeld = true;
  };
  const releasePointer = () => {
    pointerHeld = false;
  };
  const watchInput = (el: HTMLElement) => {
    const passive = { passive: true };
    el.addEventListener("wheel", noteInput, passive);
    el.addEventListener("touchmove", noteInput, passive);
    el.addEventListener("keydown", noteInput);
    el.addEventListener("pointerdown", holdPointer);
    window.addEventListener("pointerup", releasePointer);
    window.addEventListener("pointercancel", releasePointer);
    onCleanup(() => {
      el.removeEventListener("wheel", noteInput);
      el.removeEventListener("touchmove", noteInput);
      el.removeEventListener("keydown", noteInput);
      el.removeEventListener("pointerdown", holdPointer);
      window.removeEventListener("pointerup", releasePointer);
      window.removeEventListener("pointercancel", releasePointer);
    });
  };
  const followBottom = () => {
    if (frame !== null) return;
    frame = requestAnimationFrame(() => {
      frame = null;
      if (!scrollEl) return;
      scrollEl.scrollTop = scrollEl.scrollHeight;
      lastTop = scrollEl.scrollTop;
      lastHeight = scrollEl.scrollHeight;
    });
  };
  onCleanup(() => {
    if (frame !== null) cancelAnimationFrame(frame);
  });
  const jumpToLatest = () => {
    setPinned(true);
    setShowJump(false);
    followBottom();
  };
  const scrollToMessage = (messageId: string): HTMLElement | null => {
    const el = scrollEl?.querySelector<HTMLElement>(
      `[data-message-id="${CSS.escape(messageId)}"]`,
    );
    if (!el) return null;
    // Detach *before* scrolling: `followBottom` coalesces to a frame, so a pinned follow
    // scheduled by the same message change would otherwise land after this and win.
    setPinned(false);
    setShowJump(true);
    el.scrollIntoView({ block: "center", behavior: "smooth" });
    return el;
  };
  const onScroll = () => {
    if (!scrollEl) return;
    const { scrollTop: top, scrollHeight: height, clientHeight } = scrollEl;
    setPinned(
      nextPinned({
        pinned: untrack(pinned),
        operator: pointerHeld || performance.now() - lastInput < INTENT_MS,
        lastTop,
        lastHeight,
        top,
        height,
        clientHeight,
      }),
    );
    setShowJump(height - top - clientHeight > JUMP_PX);
    lastTop = top;
    lastHeight = height;
  };

  let observer: ResizeObserver | undefined;
  onCleanup(() => observer?.disconnect());
  const observeContent = (el: HTMLElement) => {
    observer?.disconnect();
    observer = new ResizeObserver(() => {
      if (pinned()) followBottom();
    });
    observer.observe(el);
  };

  // The operator initiating a turn (send / regenerate / edit) re-attaches the
  // follow, so the new answer is tracked even if they had scrolled up.
  let wasSending = false;
  createEffect(() => {
    const sending = source.sending();
    if (sending && !wasSending) jumpToLatest();
    wasSending = sending;
  });

  // Switching threads re-attaches and jumps to the latest message.
  createEffect(() => {
    source.conversationId();
    jumpToLatest();
  });

  return {
    ref: (el) => {
      scrollEl = el;
      watchInput(el);
    },
    contentRef: observeContent,
    onScroll,
    showJump,
    jumpToLatest,
    scrollToMessage,
    element: () => scrollEl,
  };
}
