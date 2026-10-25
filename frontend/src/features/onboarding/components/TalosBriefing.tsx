import { createEffect, createSignal, on, onCleanup, type JSX } from "solid-js";
import { Text } from "~/ui";
import { AnswerText } from "~/features/chat/components/AnswerText";

/** Characters added per tick, and the tick: about 190 a second, a fast model's pace. */
const CHUNK = 3;
const TICK_MS = 16;

function commonPrefix(a: string, b: string): number {
  let i = 0;
  while (i < a.length && i < b.length && a[i] === b[i]) i++;
  return i;
}

/** Talos speaking: the briefing streams in through the answer's own reveal, so it
 *  arrives the way Talos's replies do. When the briefing is rewritten, the part both
 *  versions share stays and only the rest is spoken again. */
export function TalosBriefing(props: { text: string }): JSX.Element {
  const reduced =
    typeof window !== "undefined" &&
    window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const [shown, setShown] = createSignal(reduced ? props.text : "");
  let timer: ReturnType<typeof setInterval> | null = null;
  const stop = () => {
    if (timer !== null) clearInterval(timer);
    timer = null;
  };
  onCleanup(stop);

  createEffect(
    on(
      () => props.text,
      (text) => {
        stop();
        if (reduced) {
          setShown(text);
          return;
        }
        let at = commonPrefix(shown(), text);
        setShown(text.slice(0, at));
        timer = setInterval(() => {
          at = Math.min(text.length, at + CHUNK);
          setShown(text.slice(0, at));
          if (at >= text.length) stop();
        }, TICK_MS);
      },
    ),
  );

  return (
    <section aria-label="Talos">
      <Text
        variant="body"
        as="div"
        aria-busy={shown().length < props.text.length}
      >
        <AnswerText
          text={shown()}
          active
          streaming={shown().length < props.text.length}
        />
      </Text>
    </section>
  );
}
