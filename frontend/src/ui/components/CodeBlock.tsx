import { createMemo, createResource, For, Show, type JSX } from "solid-js";
import { cx } from "../cx";
import { fontStepSize } from "./fontScale";
import { highlightToHtml } from "./highlight";

export interface CodeBlockProps {
  /** The source text to display, verbatim. */
  code: string;
  class?: string;
  /** Forwards the scrolling root element — lets a caller hook up scroll-position
   *  persistence (e.g. `rememberScroll`) without CodeBlock owning that concern.
   *  Same seam `DiffView` exposes, so a caller can swap between the two. */
  ref?: (el: HTMLDivElement) => void;
  /** Optional language for syntax highlighting (an alias `highlight.ts`
   *  recognizes, e.g. "ts", "python", "yaml"). Omitted, unrecognized, or still
   *  loading -> the plain, unhighlighted rendering below (zero layout shift —
   *  same table/gutter structure either way). */
  lang?: string;
  /** Zoom step (-2..+2), matching the View panel's font-size control. Default 0. */
  fontStep?: number;
  /** Wraps long lines instead of the default horizontal scroll. Default false. */
  softWrap?: boolean;
}

/** A monospace code view with a dim, tabular line-number gutter. Optionally
 *  syntax-highlighted (lazily, via Shiki) when `lang` is set and recognized;
 *  otherwise a faithful, scrollable plain rendering that fills its container. */
export function CodeBlock(props: CodeBlockProps): JSX.Element {
  // Split once per render; a trailing newline shouldn't add a phantom blank line.
  const lines = (): string[] => {
    const text = props.code.replace(/\n$/, "");
    return text.length === 0 ? [""] : text.split("\n");
  };

  // Highlighted lines, parsed out of Shiki's `<pre class="shiki"><code><span
  // class="line">…</span>\n…</code></pre>` output — one inner-HTML string per
  // source line, reused inside the SAME table/gutter cells the plain path
  // renders, so there's no layout shift when highlighting resolves.
  const [highlightedLines] = createResource(
    () => (props.lang ? { code: props.code, lang: props.lang } : undefined),
    async ({ code, lang }): Promise<string[] | null> => {
      const html = await highlightToHtml(code, lang);
      if (!html) return null;
      const doc = new DOMParser().parseFromString(html, "text/html");
      const spans = Array.from(doc.querySelectorAll("code > .line"));
      return spans.length > 0 ? spans.map((s) => s.innerHTML) : null;
    },
  );

  const size = createMemo(() => fontStepSize(props.fontStep));
  const cellWrap = (): string =>
    props.softWrap ? "whitespace-pre-wrap break-words" : "whitespace-pre";

  return (
    <div
      ref={(el) => props.ref?.(el)}
      class={cx(
        "h-full overflow-auto bg-surface font-mono text-body",
        props.class,
      )}
      style={{ "font-size": `${size()}px` }}
    >
      <table class="w-full border-collapse">
        <tbody>
          <For each={lines()}>
            {(line, i) => (
              <tr>
                <td class="select-none whitespace-nowrap px-3 py-0 text-right align-top text-dim tabular-nums">
                  {i() + 1}
                </td>
                <Show
                  when={highlightedLines()?.[i()]}
                  fallback={
                    <td
                      class={cx(
                        "w-full py-0 pr-3 align-top text-text",
                        cellWrap(),
                      )}
                    >
                      {line || " "}
                    </td>
                  }
                >
                  {(html) => (
                    <td
                      class={cx(
                        "w-full py-0 pr-3 align-top text-text",
                        cellWrap(),
                      )}
                      // Shiki-escaped token spans over our own line text, never
                      // the operator's raw markup.
                      innerHTML={html()}
                    />
                  )}
                </Show>
              </tr>
            )}
          </For>
        </tbody>
      </table>
    </div>
  );
}
