import { createMemo, createSignal, For, type JSX } from "solid-js";
import { cx } from "~/ui";
import { frontmatterLineCount } from "../model";

const TEXT = "font-mono text-code leading-5 whitespace-pre";

/** A monospace text editor with a line-number gutter. A coloured copy of the
 *  text sits behind a transparent textarea, so the frontmatter lines (between
 *  the `---` fences) read in the info accent while the caret and selection stay
 *  the textarea's own. Lines never wrap, which is what keeps the three layers
 *  aligned; they scroll together. */
export function CodeEditor(props: {
  value: string;
  onInput: (value: string) => void;
  readOnly?: boolean;
  label: string;
}): JSX.Element {
  const [scroll, setScroll] = createSignal({ top: 0, left: 0 });
  const lines = createMemo(() => props.value.split("\n"));
  const front = createMemo(() => frontmatterLineCount(props.value));

  return (
    <div class="relative flex h-full min-h-72 border-0 bg-surface">
      <div
        aria-hidden="true"
        class={cx(
          TEXT,
          "w-10 shrink-0 overflow-hidden border-r border-line py-3 pr-2 text-right text-dim select-none",
        )}
      >
        <div style={{ transform: `translateY(${-scroll().top}px)` }}>
          <For each={lines()}>{(_, i) => <div>{i() + 1}</div>}</For>
        </div>
      </div>
      <div class="relative min-w-0 flex-1">
        <div
          aria-hidden="true"
          class={cx(
            TEXT,
            "pointer-events-none absolute inset-0 overflow-hidden p-3 text-text",
          )}
        >
          <div
            style={{
              transform: `translate(${-scroll().left}px, ${-scroll().top}px)`,
            }}
          >
            <For each={lines()}>
              {(line, i) => (
                <div class={i() < front() ? "text-info" : undefined}>
                  {line || " "}
                </div>
              )}
            </For>
          </div>
        </div>
        <textarea
          aria-label={props.label}
          value={props.value}
          readOnly={props.readOnly}
          spellcheck={false}
          wrap="off"
          onInput={(e) => props.onInput(e.currentTarget.value)}
          onScroll={(e) =>
            setScroll({
              top: e.currentTarget.scrollTop,
              left: e.currentTarget.scrollLeft,
            })
          }
          class={cx(
            TEXT,
            "scrollbar-thin absolute inset-0 h-full w-full resize-none overflow-auto bg-transparent p-3 text-transparent caret-bright outline-none selection:bg-raised",
          )}
        />
      </div>
    </div>
  );
}
