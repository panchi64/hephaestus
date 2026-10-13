import type { MarkedExtension, TokenizerAndRendererExtension } from "marked";

/**
 * Diagram fences for the `marked` pipeline (see Markdown.tsx).
 *
 * A ```` ```mermaid ```` block is claimed here before `marked`'s own fenced-code
 * tokenizer sees it, and rendered as an **empty placeholder element** carrying
 * its source in a data attribute. Nothing is drawn at parse time and mermaid is
 * not imported: `hydrateMermaid` picks the placeholder up afterwards, modelled
 * on how `remoteImages` picks up an `<img>` with no `src`.
 *
 * That split is what keeps `cachedParse` synchronous. Markdown.tsx parses a
 * block to an HTML *string* and caches it by source; a renderer that had to
 * await a layout engine could not participate in that at all, and making the
 * whole parse async would turn every streaming delta into a frame of blank
 * prose.
 *
 * **An unterminated fence is the normal case, not an error.** While the model is
 * typing, the last block in the answer is a fence with no closer, and it stays
 * that way for as many deltas as the diagram takes to write. It is marked
 * `data-mermaid-partial` and the hydrator renders a quiet frame for it — never a
 * parse error, which would otherwise flash on screen for the whole time the
 * model is drawing.
 *
 * The fence's info string past the word `mermaid` is taken as the legend for the
 * frame's header band (```` ```mermaid Call path ````). It is a caption the
 * model wrote, so it is escaped into an attribute like everything else here and
 * rendered as `plate` text, never as markup.
 */

/** Opening fence: up to three spaces of indent, three or more backticks or
 *  tildes, the word `mermaid`, and an optional legend to end of line. */
const FENCE_OPEN = /^ {0,3}(`{3,}|~{3,})[ \t]*mermaid[ \t]*([^\n]*)\n/;

/** Where the next diagram fence could start, for `marked`'s block scanner — it
 *  uses this to cut a paragraph short rather than swallowing the fence into it. */
const FENCE_START = /(?:^|\n) {0,3}(?:`{3,}|~{3,})[ \t]*mermaid/;

/** Escape for an HTML *attribute* value.
 *
 *  Local rather than shared with `markdownLinks`, which keeps its own copy
 *  private: both are three lines of the same five replacements, and the shared
 *  version would be a module existing only to be imported twice. The source that
 *  goes through here is model-authored diagram text, so the quoting is
 *  load-bearing — an unescaped `"` in a node label would end the attribute and
 *  put the rest of the diagram into the tag. */
function escapeAttr(value: string): string {
  return value
    .replace(/&/g, "&amp;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

interface MermaidToken {
  type: "mermaidDiagram";
  raw: string;
  text: string;
  legend: string;
  /** The fence was closed. False while the model is still writing it. */
  complete: boolean;
}

const mermaidDiagram: TokenizerAndRendererExtension = {
  name: "mermaidDiagram",
  level: "block",
  start(src) {
    const match = FENCE_START.exec(src);
    if (!match) return undefined;
    // The pattern may have matched the leading newline; point at the fence.
    return match.index + (match[0].startsWith("\n") ? 1 : 0);
  },
  tokenizer(src) {
    const open = FENCE_OPEN.exec(src);
    if (!open) return undefined;
    const marker = open[1];
    const legend = open[2].trim();
    const body = src.slice(open[0].length);
    // The closer is the same character, at least as many times. Neither ` nor ~
    // is a regex metacharacter, so the marker interpolates as written.
    const close = new RegExp(
      `^([\\s\\S]*?)\\n {0,3}${marker[0]}{${marker.length},}[ \\t]*(?:\\n|$)`,
    ).exec(body);
    if (close)
      return {
        type: "mermaidDiagram",
        raw: open[0] + close[0],
        text: close[1],
        legend,
        complete: true,
      } satisfies MermaidToken;
    // No closer anywhere in what has arrived, so the fence runs to the end of
    // the source — exactly what `marked`'s own fenced-code tokenizer does at
    // EOF, and the reason `raw` is the whole remainder.
    return {
      type: "mermaidDiagram",
      raw: src,
      text: body,
      legend,
      complete: false,
    } satisfies MermaidToken;
  },
  renderer(token) {
    const { text, legend, complete } = token as unknown as MermaidToken;
    return [
      `<div class="heph-mermaid"`,
      ` data-mermaid-src="${escapeAttr(text)}"`,
      legend ? ` data-mermaid-legend="${escapeAttr(legend)}"` : "",
      complete ? "" : ` data-mermaid-partial=""`,
      `></div>`,
    ].join("");
  },
};

/** Pass to `marked.use(...)` to turn ```` ```mermaid ```` fences into diagram
 *  placeholders for `hydrateMermaid` to draw into. */
export const markedMermaid: MarkedExtension = {
  extensions: [mermaidDiagram],
};
