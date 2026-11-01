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
import { INTERVAL_SEED, REVEAL_MS, nextInterval } from "../streamReveal";
import { stableStreamSource } from "../streamTail";
import {
  applyReveal,
  createRevealState,
  revealEndsAt,
  unwrapAll,
} from "./answerReveal";

/** Slack after the last fade before the terminal flush: two frames at 60 Hz, so the
 *  fade is over on screen and not only by the clock. */
const FLUSH_SLACK_MS = 32;

/** A passage of the answer — full-width and bright. The active, still-streaming
 *  passage carries the caret and resolves each arriving character in on its own
 *  schedule (§8, human register).
 *
 *  The reveal is scheduled in absolute time because each delta re-parses the trailing
 *  block and destroys anything mid-animation; a character that knows when it started
 *  resumes its fade. See `streamReveal.ts`. */
export function AnswerText(props: {
  text: string;
  active?: boolean;
  streaming?: boolean;
}): JSX.Element {
  const live = () => Boolean(props.active && props.streaming);
  const screenshotSrc = useScreenshotSrc();

  // Latched: once a passage has streamed it keeps the block path. `Markdown` renders a
  // different element per path, so flipping the flag as the run ends would rebuild the
  // whole answer, a visible flash just as the operator starts reading. A message that
  // never streamed never takes the block path.
  const [streamStable, setStreamStable] = createSignal(live());
  createEffect(() => {
    if (live()) setStreamStable(true);
  });
  // While live, the unstable tail is held back so nothing on screen changes shape.
  const source = createMemo(() =>
    live() ? stableStreamSource(props.text) : props.text,
  );

  let host: HTMLDivElement | undefined;
  // A message that arrives complete never runs a pass, so never animates.
  const reveal = createRevealState();
  // Running estimate of the gap between deltas, which paces the stagger. Seeded,
  // since the first delta has nothing to measure against.
  let interval = INTERVAL_SEED;
  let lastDelta = 0;
  // The terminal flush. A block gives its wrappers back on the delta after it stops
  // trailing, but the last block never does, so this unwraps it once the text stops.
  // Re-armed on every delta.
  let flush: ReturnType<typeof setTimeout> | null = null;
  onCleanup(() => {
    if (flush !== null) clearTimeout(flush);
  });

  createEffect(() => {
    // Every delta, not every change to `source()`: a held line leaves the shown text
    // unchanged for several deltas, and the reveal still advances through them.
    void props.text;
    // A passage that streamed gets one more pass after `live()` drops, because its
    // last characters land in the tick the stream closes.
    if (!host || (!live() && !reveal.seeded)) return;
    // Let Markdown commit its own DOM for this delta first.
    queueMicrotask(() => {
      if (!host) return;
      const now = performance.now();
      if (lastDelta) interval = nextInterval(interval, now - lastDelta);
      lastDelta = now;
      applyReveal(host, reveal, now, interval);

      // Unwrapping ends the fade visually, so firing a frame early is a visible snap.
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
      <div
        ref={host}
        class="inline"
        style={{ "--token-reveal-ms": `${REVEAL_MS}ms` }}
      >
        {/* `copyCode` stays on while streaming. Turning it on at the end would move
            every `pre` and `table` into a wrapper just as the operator starts
            reading; the buttons stay hidden until hover. */}
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
