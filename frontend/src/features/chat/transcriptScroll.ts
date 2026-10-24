/**
 * How the transcript scrolls — the one piece of genuinely stateful behaviour it has.
 *
 * **Read in place.** A send seats the operator's message at the top of the view and
 * reserves the screen below it, so the reply grows downward under a still view. Nothing
 * scrolls on the operator's behalf while it streams; once the reply runs past the bottom
 * edge, the jump control offers the rest, and taking it follows the end from there.
 *
 * **The anchor.** Whenever the view is not following the bottom, one element — the live
 * answer if any of it is on screen, else the topmost visible turn — is held at the same
 * viewport offset. Reasoning clearing, a fold, the progress rail leaving or an image
 * decoding above it are corrected in the same frame, before paint, whatever caused them.
 *
 * **The follow.** Opening a thread, or taking the jump control, pins the view to the
 * bottom: late layout (images, a reloaded turn's failure opening) keeps the newest turn in
 * view, and a long run can be watched at its end. Any upward scroll the operator makes
 * lets go. A send lets go of the opening pin but keeps one the operator took
 * (`nextFollow`).
 */

import { createEffect, createSignal, on, onCleanup, untrack } from "solid-js";
import { isNamedInPlace } from "./stream/binding";
import {
  firstReaching,
  nextFollow,
  nextPinned,
  pickAnchor,
  roomFor,
  type FollowEvent,
} from "./transcriptScrollRules";

/** Past roughly one screenful the jump-to-latest control appears. */
const JUMP_PX = 240;
/** While a turn streams, this much unseen text below the view raises "New text below". */
const NEW_BELOW_PX = 24;
/** A scroll this soon after the operator's last wheel, touch or key is theirs. Covers
 *  smooth keyboard scrolling, which keeps moving after the keydown. */
const INTENT_MS = 600;
/** Frames to wait for a just-sent message to render before seating it. */
const SEAT_ATTEMPTS = 4;

export interface TranscriptFollow {
  /** `ref` for the scrolling transcript container. */
  ref: (el: HTMLDivElement) => void;
  /** `ref` for the content inside the container; its size changes drive the follow
   *  and the anchor. Must not carry `room()`, or growth inside the room goes unseen.
   *  Each turn's `[data-message-id]` root must be its direct child. */
  contentRef: (el: HTMLElement) => void;
  /** `ref` for a zero-height marker after the last turn: where the content ends, as
   *  opposed to where the reserved room ends. */
  endRef: (el: HTMLElement) => void;
  /** `onScroll` for the same container. */
  onScroll: () => void;
  /** Minimum height, in px, for the element wrapping the content — the room a send
   *  reserves below the operator's message — or null for none. */
  room: () => number | null;
  /** Whether the jump control is up. */
  showJump: () => boolean;
  /** Whether it is up because the streaming reply has run past the bottom edge. */
  newBelow: () => boolean;
  /** The jump control: scroll to the end and follow it, through later sends too, until
   *  the operator scrolls up. */
  followLatest: () => void;
  /** Scroll to the end and follow its growth, without changing what a send does. */
  jumpToLatest: () => void;
  /** Bring one turn into view and **detach the follow**, answering with its element —
   *  or `null` when this transcript does not hold it. Detaching first matters: a pinned
   *  follow would put the operator straight back at the bottom on the next growth. */
  scrollToMessage: (messageId: string) => HTMLElement | null;
  /** The container itself, for the keymap's focus targets. */
  element: () => HTMLDivElement | undefined;
}

export function createTranscriptFollow(source: {
  sending: () => boolean;
  /** The open thread; a switch re-attaches and jumps to the latest message. */
  conversationId: () => string | null;
  /** The thread the stream is bound to, which tells a new thread being named after its
   *  first run from a switch to another thread. */
  boundId: () => string | null;
  /** The newest operator message, which a send seats at the top of the view. */
  anchorTurnId: () => string | null;
}): TranscriptFollow {
  let scrollEl: HTMLDivElement | undefined;
  let contentEl: HTMLElement | undefined;
  let endEl: HTMLElement | undefined;
  const [pinned, setPinned] = createSignal(true);
  let chosen = false;
  const apply = (event: FollowEvent) => {
    const was = untrack(pinned);
    const next = nextFollow({ pinned: was, chosen }, event);
    chosen = next.chosen;
    setPinned(next.pinned);
    // Scrolls are not anchored while pinned, so letting go anchors here.
    if (was && !next.pinned && scrollEl) recordAnchor(scrollEl);
  };
  const [below, setBelow] = createSignal(0);
  const [room, setRoom] = createSignal<number | null>(null);
  // The turn the room is sized for, and its offset into the content when last measured.
  let roomEl: HTMLElement | null = null;
  let roomOffset = 0;

  // The container as of the last thing that moved it, so `nextPinned` can tell the
  // operator's scroll from one this module wrote.
  let lastTop = 0;
  let lastHeight = 0;
  let lastInput = Number.NEGATIVE_INFINITY;
  let pointerHeld = false;

  let anchor: HTMLElement | null = null;
  let anchorTop = 0;

  // The scroller's padding belongs to the overlays (title above, dock fade below), so
  // the readable view is the box inside it. Re-read on resize: the title inset moves.
  let insetTop = 0;
  let insetBottom = 0;
  const readInsets = (el: HTMLElement) => {
    const style = getComputedStyle(el);
    insetTop = parseFloat(style.paddingTop) || 0;
    insetBottom = parseFloat(style.paddingBottom) || 0;
  };
  type View = { top: number; bottom: number };
  const view = (el: HTMLElement): View => {
    const rect = el.getBoundingClientRect();
    return { top: rect.top + insetTop, bottom: rect.bottom - insetBottom };
  };

  const noteMove = (el: HTMLElement) => {
    lastTop = el.scrollTop;
    lastHeight = el.scrollHeight;
  };
  const measureBelow = (el: HTMLElement, v: View = view(el)) => {
    if (!endEl) return;
    setBelow(Math.max(0, endEl.getBoundingClientRect().top - v.bottom));
  };

  /** Turn roots from the first one reaching into the view, each followed by the live
   *  answer inside it — document order, so `pickAnchor` reads them top-down. */
  const candidates = (top: number, bottom: number): HTMLElement[] => {
    // The turns are the content's direct children: walking those is O(turns), where a
    // selector query walks every node of every turn on each scroll event.
    const turns: HTMLElement[] = [];
    for (const child of contentEl?.children ?? [])
      if (child instanceof HTMLElement && child.hasAttribute("data-message-id"))
        turns.push(child);
    const from = firstReaching(
      turns.length,
      (i) => turns[i].getBoundingClientRect().bottom,
      top,
    );
    const out: HTMLElement[] = [];
    for (let i = from; i < turns.length; i++) {
      const turn = turns[i];
      if (turn.getBoundingClientRect().top >= bottom) break;
      out.push(turn);
      const live = turn.querySelector<HTMLElement>("[data-live-answer]");
      if (live) out.push(live);
    }
    return out;
  };
  const recordAnchor = (el: HTMLElement, v: View = view(el)) => {
    const { top, bottom } = v;
    const els = candidates(top, bottom);
    const boxes = els.map((c) => {
      const rect = c.getBoundingClientRect();
      return {
        top: rect.top,
        bottom: rect.bottom,
        live: c.hasAttribute("data-live-answer"),
      };
    });
    const i = pickAnchor(boxes, top, bottom);
    anchor = i >= 0 ? els[i] : null;
    anchorTop = i >= 0 ? boxes[i].top : 0;
  };
  /** After a correction, keep the anchor while it is still on screen — re-picking
   *  when the run ends would hand it to the turn above just as the progress rail
   *  collapses over the answer. A live answer coming into view still takes it over. */
  const refreshAnchor = (el: HTMLElement, v: View = view(el)) => {
    const { top, bottom } = v;
    const onScreen = (c: HTMLElement | null): c is HTMLElement => {
      if (!c?.isConnected) return false;
      const rect = c.getBoundingClientRect();
      return rect.bottom > top && rect.top < bottom;
    };
    const live = el.querySelector<HTMLElement>("[data-live-answer]");
    const next = onScreen(live) ? live : onScreen(anchor) ? anchor : null;
    if (!next) return recordAnchor(el, v);
    // The same anchor keeps its target offset: re-reading it here would bank every
    // sub-pixel drift `settle` declined to correct, and a collapse is many of those.
    if (next === anchor) return;
    anchor = next;
    anchorTop = next.getBoundingClientRect().top;
  };

  const turnEl = (id: string | null): HTMLElement | null =>
    id
      ? (scrollEl?.querySelector<HTMLElement>(
          `[data-message-id="${CSS.escape(id)}"]`,
        ) ?? null)
      : null;

  const offsetOf = (turn: HTMLElement): number =>
    contentEl
      ? turn.getBoundingClientRect().top - contentEl.getBoundingClientRect().top
      : 0;
  const layoutRoom = (el: HTMLElement, turn: HTMLElement) => {
    roomEl = turn;
    roomOffset = offsetOf(turn);
    setRoom(roomFor(roomOffset, el.clientHeight, insetTop, insetBottom));
  };
  /** Re-size the room for its turn, or drop it once that turn is gone. A turn
   *  re-rendered under a new id is found again where it was; finding only an earlier
   *  one means the seated turn was deleted or rewound away. */
  const keepRoom = (el: HTMLElement) => {
    if (!roomEl) return;
    let turn: HTMLElement | null = roomEl;
    if (!turn.isConnected) {
      const latest = turnEl(source.anchorTurnId());
      turn = latest && offsetOf(latest) >= roomOffset - 1 ? latest : null;
    }
    if (turn) return layoutRoom(el, turn);
    roomEl = null;
    setRoom(null);
  };

  /** Run after any layout change: follow the bottom, or put the anchor back where it
   *  was. Called from a ResizeObserver, so the write lands before paint. */
  const settle = () => {
    if (!scrollEl) return;
    keepRoom(scrollEl);
    if (untrack(pinned)) {
      scrollEl.scrollTop = scrollEl.scrollHeight;
      noteMove(scrollEl);
      measureBelow(scrollEl);
      return;
    }
    if (anchor?.isConnected) {
      const drift = anchor.getBoundingClientRect().top - anchorTop;
      if (Math.abs(drift) >= 0.5) {
        scrollEl.scrollTop += drift;
        noteMove(scrollEl);
      }
    }
    const v = view(scrollEl);
    refreshAnchor(scrollEl, v);
    measureBelow(scrollEl, v);
  };

  const watchInput = (el: HTMLElement) => {
    const noteInput = () => {
      lastInput = performance.now();
    };
    const holdPointer = () => {
      pointerHeld = true;
    };
    const releasePointer = () => {
      pointerHeld = false;
    };
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

  /** Reserve the room under the operator's newest message and scroll it to the top.
   *  A send flips `sending` before its turn is in the DOM, so this waits a frame or few. */
  let seatFrame: number | null = null;
  const seatTurn = (attempt = 0) => {
    if (seatFrame !== null) cancelAnimationFrame(seatFrame);
    seatFrame = requestAnimationFrame(() => {
      seatFrame = null;
      const turn = turnEl(source.anchorTurnId());
      if (!scrollEl || !turn) {
        if (attempt < SEAT_ATTEMPTS) seatTurn(attempt + 1);
        return;
      }
      readInsets(scrollEl);
      layoutRoom(scrollEl, turn);
      const v = view(scrollEl);
      scrollEl.scrollTop += turn.getBoundingClientRect().top - v.top;
      noteMove(scrollEl);
      recordAnchor(scrollEl, v);
      measureBelow(scrollEl, v);
    });
  };

  // A swap that leaves every observed size unchanged can still move the anchor, and no
  // ResizeObserver reports it, so any change to the content also settles once, on the
  // next frame. Mutations stop while a run waits on a tool, and so does this.
  let settleFrame: number | null = null;
  const settleSoon = () => {
    if (settleFrame !== null) return;
    settleFrame = requestAnimationFrame(() => {
      settleFrame = null;
      settle();
    });
  };

  const observers: { disconnect: () => void }[] = [];
  onCleanup(() => {
    observers.forEach((o) => o.disconnect());
    if (seatFrame !== null) cancelAnimationFrame(seatFrame);
    if (settleFrame !== null) cancelAnimationFrame(settleFrame);
  });
  const observe = (el: HTMLElement, onResize: () => void) => {
    const observer = new ResizeObserver(onResize);
    observer.observe(el);
    observers.push(observer);
  };

  const jumpToLatest = () => {
    apply("jump");
    settle();
  };
  const followLatest = () => {
    apply("follow");
    settle();
  };

  const scrollToMessage = (messageId: string): HTMLElement | null => {
    const el = turnEl(messageId);
    if (!el) return null;
    apply("letGo");
    el.scrollIntoView({ block: "center", behavior: "smooth" });
    return el;
  };

  const onScroll = () => {
    if (!scrollEl) return;
    const { scrollTop: top, scrollHeight: height } = scrollEl;
    // The event this module's own write provokes arrives at the position it wrote;
    // re-picking then would hand the anchor away from the text it just held still.
    const ours = Math.abs(top - lastTop) < 1;
    const keep = nextPinned({
      pinned: untrack(pinned),
      operator: pointerHeld || performance.now() - lastInput < INTENT_MS,
      lastTop,
      lastHeight,
      top,
      height,
    });
    if (!keep) apply("letGo");
    lastTop = top;
    lastHeight = height;
    const v = view(scrollEl);
    // A pinned `settle` follows the bottom and never reads the anchor.
    if (!untrack(pinned)) {
      if (ours) refreshAnchor(scrollEl, v);
      else recordAnchor(scrollEl, v);
    }
    measureBelow(scrollEl, v);
  };

  let wasSending = false;
  createEffect(() => {
    const sending = source.sending();
    if (sending && !wasSending) {
      apply("send");
      if (!chosen) seatTurn();
    }
    wasSending = sending;
  });

  createEffect(
    on(source.conversationId, (id, prev) => {
      if (isNamedInPlace(id, prev, untrack(source.boundId))) return;
      setRoom(null);
      roomEl = null;
      anchor = null;
      apply("open");
      settle();
    }),
  );

  return {
    ref: (el) => {
      scrollEl = el;
      watchInput(el);
      readInsets(el);
      observe(el, () => {
        readInsets(el);
        settle();
      });
    },
    contentRef: (el) => {
      contentEl = el;
      observe(el, settle);
      const mutations = new MutationObserver(settleSoon);
      mutations.observe(el, {
        childList: true,
        subtree: true,
        characterData: true,
        attributes: true,
      });
      observers.push(mutations);
    },
    endRef: (el) => {
      endEl = el;
    },
    onScroll,
    room,
    showJump: () =>
      below() > JUMP_PX || (source.sending() && below() > NEW_BELOW_PX),
    newBelow: () => source.sending() && below() > NEW_BELOW_PX,
    followLatest,
    jumpToLatest,
    scrollToMessage,
    element: () => scrollEl,
  };
}
