import { createMemo, For, Show, type JSX } from "solid-js";
import { cx } from "../cx";
import { highlightCode, splitHighlightedLines } from "./codeHighlight";
import { fontStepSize } from "./fontScale";

export interface CodeBlockProps {
  /** The source text to display, verbatim. */
  code: string;
  class?: string;
  /** Forwards the scrolling root, for scroll persistence such as `rememberScroll`.
   *  `DiffView` exposes the same seam, so a caller can swap between the two. */
  ref?: (el: HTMLDivElement) => void;
  /** Optional language for syntax highlighting: a highlight.js name or alias, such
   *  as a file extension ("ts", "py", "yml"). Omitted or unrecognized renders plain,
   *  in the same table/gutter structure. */
  lang?: string;
  /** Zoom step (-2..+2), matching the View panel's font-size control. Default 0. */
  fontStep?: number;
  /** Wraps long lines instead of the default horizontal scroll. Default false. */
  softWrap?: boolean;
}

/** A monospace code view with a dim, tabular line-number gutter, syntax-highlighted
 *  when `lang` is set and recognized; otherwise a faithful, scrollable plain
 *  rendering that fills its container. */
export function CodeBlock(props: CodeBlockProps): JSX.Element {
  // A trailing newline shouldn't add a phantom blank line.
  const text = createMemo(() => props.code.replace(/\n$/, ""));
  const lines = createMemo(() => text().split("\n"));

  const highlightedLines = createMemo(() => {
    const html = props.lang ? highlightCode(text(), props.lang) : null;
    return html === null ? null : splitHighlightedLines(html);
  });

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
                      // highlight.js-escaped token spans over our own line text,
                      // never the operator's raw markup.
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
