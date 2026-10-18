import {
  Show,
  createEffect,
  createMemo,
  createSignal,
  onCleanup,
  type JSX,
} from "solid-js";
import { Caret, Markdown } from "~/ui";
import { useScreenshotSrc } from "../screenshots";
import { INTERVAL_SEED, nextInterval } from "../streamReveal";
import { stableStreamSource } from "../streamTail";
import {
  applyReveal,
  createRevealState,
  revealEndsAt,
  unwrapAll,
} from "./answerReveal";

/** Slack past the last character's `REVEAL_MS` before the terminal flush unwraps it —
 *  two frames at 60Hz, enough that the fade is over on any display rather than merely
 *  over according to the clock. */
const FLUSH_SLACK_MS = 32;

/** A passage of the answer — full-width and bright. The active, still-streaming
 *  passage carries the caret, resolves each arriving character in on its own
 *  schedule (§8, human register), and defers code-copy enhancement until it
 *  settles.
 *
 *  `streamStable` while live keeps the DOM of every settled block, so only the
 *  trailing block re-parses per delta — which is what stops KaTeX/markdown
 *  flicker. It is also why the reveal is scheduled in absolute time: that
 *  re-parse destroys anything mid-animation, and only a character that knows
 *  when it *started* can pick its fade back up rather than restarting it. See
 *  `streamReveal.ts`. */
export function AnswerText(props: {
  text: string;
  active?: boolean;
  streaming?: boolean;
}): JSX.Element {
  const live = () => Boolean(props.active && props.streaming);
  const screenshotSrc = useScreenshotSrc();

  /* Latched, and this is what stops the screen flashing when a run finishes.
     `Markdown` renders a *different element* for `streamStable` than for its
     default path — it has to, since Solid's `innerHTML` prop can't coexist with
     rendered children on one node. Passing `live()` straight through therefore
     flipped that flag at the exact moment the answer completed, and Solid tore
     down the entire rendered answer and rebuilt it: a full re-parse, a fresh
     DOM, KaTeX re-rendered, every `pre` and `table` re-wrapped. On a long answer
     that is a visible flash at the worst possible moment — the instant the
     operator starts reading.

     Once a passage has streamed it keeps the block path forever. The two paths
     render the same content (the block path exists to replicate the prose
     cascade at the block-wrapper level), so there is nothing to switch back
     for. A message that never streamed — history, a settled turn — never takes
     the block path at all, which is the cheaper read for a long transcript. */
  const [streamStable, setStreamStable] = createSignal(live());
  createEffect(() => {
    if (live()) setStreamStable(true);
  });
  // While live, the unstable tail is held back so nothing on screen changes shape.
  const source = createMemo(() =>
    live() ? stableStreamSource(props.text) : props.text,
  );

  let host: HTMLDivElement | undefined;
  // The reveal's carry-over between deltas — which block is being wrapped, that block's
  // schedule, and when each earlier block's wrappers may come out. A message that
  // arrives complete (history, a settled turn) never runs a pass and so never animates.
  const reveal = createRevealState();
  // Running estimate of the gap between deltas, which is what the stagger is
  // paced against — see `streamReveal.ts`. Seeded rather than measured from the
  // first delta, since there is nothing to measure against yet.
  let interval = INTERVAL_SEED;
  let lastDelta = 0;
  // The terminal flush. A settled block gives its wrappers back on the delta after it
  // stops being the trailing one, but the LAST block of an answer never stops being
  // trailing — no further delta arrives to notice — so without this every answer keeps
  // its final block's spans forever. Re-armed on every delta and cleared on cleanup, so
  // it only ever fires once the text has actually stopped.
  let flush: ReturnType<typeof setTimeout> | null = null;
  onCleanup(() => {
    if (flush !== null) clearTimeout(flush);
  });

  createEffect(() => {
    // Read the source so this effect re-runs on every delta (the value itself
    // is not needed — the DOM Markdown just committed is what gets walked).
    // Every delta, not every change to `source()`: a held line leaves the shown text
    // unchanged for several deltas, and the reveal still has to advance through them.
    void props.text;
    // A passage that never streamed (history, a settled turn) schedules nothing
    // and renders instantly. One that has streamed keeps going for a final pass
    // after `live()` drops: the run's last characters land in the same tick the
    // stream closes, and bailing here made them the one part of the answer that
    // appeared without resolving — a pop right at the end of an otherwise smooth
    // reveal.
    if (!host || (!live() && !reveal.seeded)) return;
    // Let Markdown commit its own DOM for this delta first.
    queueMicrotask(() => {
      if (!host) return;
      const now = performance.now();
      if (lastDelta) interval = nextInterval(interval, now - lastDelta);
      lastDelta = now;
      applyReveal(host, reveal, now, interval);

      // Arm the flush for just after the last scheduled character finishes. A frame of
      // slack past that rather than exactly on it: unwrapping is what ends the fade
      // visually, and ending it a frame early is a visible snap.
      if (flush !== null) clearTimeout(flush);
      const end = revealEndsAt(reveal) || now;
      flush = setTimeout(
        () => {
          flush = null;
          if (host) unwrapAll(host);
        },
        Math.max(0, end - now) + FLUSH_SLACK_MS,
      );
    });
  });

  return (
    <div>
      <div ref={host} class="inline">
        {/* `copyCode` stays on THROUGHOUT, including while streaming. It used to
            be gated on `!live()` to save a DOM scan per delta, but that gate was
            paid for at the worst moment: flipping it on completion ran the
            enhancement pass over a finished answer, and that pass *physically
            moves* every `pre` and `table` — out of the tree and back inside a
            wrapper. Re-laying-out and re-rasterizing every code block the
            instant the answer settles is a redraw the operator sees.

            Enhancing as we go does the same work incrementally on the trailing
            block instead, and leaves nothing to do at the end. The buttons are
            hidden until their block is hovered, so nothing appears mid-stream
            either. */}
        <Markdown
          class="inline"
          streamStable={streamStable()}
          resolveLocalImage={screenshotSrc}
        >
          {source()}
        </Markdown>
      </div>
      {/* Zero-height, so the caret arriving and leaving moves nothing below it. */}
      <Show when={live()}>
        <div class="h-0 overflow-visible">
          <Caret class="text-bright" />
        </div>
      </Show>
    </div>
  );
}
