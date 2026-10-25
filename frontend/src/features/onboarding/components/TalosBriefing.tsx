import { createEffect, createSignal, For, onCleanup, type JSX } from "solid-js";
import { Text } from "~/ui";
import { AnswerText } from "~/features/chat/components/AnswerText";

/** Characters added per tick, and the tick: about 190 a second, a fast model's pace. */
const CHUNK = 3;
const TICK_MS = 16;

const reducedMotion = (): boolean =>
  typeof window !== "undefined" &&
  window.matchMedia("(prefers-reduced-motion: reduce)").matches;

/** One paragraph, spoken once: it streams in through the answer's reveal when `ready`
 *  turns true, then reports itself done. */
function SpokenParagraph(props: {
  text: string;
  ready: boolean;
  onDone: () => void;
}): JSX.Element {
  const [shown, setShown] = createSignal(reducedMotion() ? props.text : "");
  let timer: ReturnType<typeof setInterval> | null = null;
  onCleanup(() => {
    if (timer !== null) clearInterval(timer);
  });

  createEffect(() => {
    if (!props.ready || timer !== null) return;
    if (shown() === props.text) {
      props.onDone();
      return;
    }
    let at = 0;
    timer = setInterval(() => {
      at = Math.min(props.text.length, at + CHUNK);
      setShown(props.text.slice(0, at));
      if (at >= props.text.length && timer !== null) {
        clearInterval(timer);
        props.onDone();
      }
    }, TICK_MS);
  });

  return (
    <AnswerText
      text={shown()}
      active
      streaming={shown().length < props.text.length}
    />
  );
}

/** Talos speaking: each paragraph streams in through the answer's own reveal, in order.
 *  Paragraphs are keyed by their text, so one that survives a rewrite keeps its element
 *  and is never spoken twice; only a changed or new paragraph is revealed. */
export function TalosBriefing(props: { text: string }): JSX.Element {
  const paragraphs = (): string[] => props.text.split("\n\n").filter(Boolean);
  const [spoken, setSpoken] = createSignal<ReadonlySet<string>>(new Set());
  const markSpoken = (text: string) => setSpoken((s) => new Set(s).add(text));

  return (
    <section aria-label="Talos" class="flex flex-col gap-3">
      <For each={paragraphs()}>
        {(text, i) => (
          <Text variant="body" as="div">
            <SpokenParagraph
              text={text}
              ready={i() === 0 || spoken().has(paragraphs()[i() - 1])}
              onDone={() => markSpoken(text)}
            />
          </Text>
        )}
      </For>
    </section>
  );
}
